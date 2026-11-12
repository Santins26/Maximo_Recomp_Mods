"""Cross-world enemy placement orchestration.

This module is the single decision point for "may this enemy be placed into
this world, and what has to happen for it to render?". It owns:

  * classification of every candidate placement into exactly one outcome
    (see ``classify_placement`` and the path constants below), and
  * reading the shared-texture-cache boundary out of ``GLOBAL.PRT``.

The porting machinery itself (``WorldPortSession``, ``run_cross_world_placements``,
``verify_session``, fallback handling) lands in later tasks and will live here too.

Two facts about the file formats drive the whole design:

A BLOB NAMES ITS RESOURCES IN ITS MATERIAL SECTION, NOT AT OFFSET 22
    ``[blob+0x20, blob+0x24)`` is framed ``[record_count u32]`` then per record
    ``[len u32][body]``, and each body opens with one or two 5-byte references
    ``[rec_type u8][u32 = flag<<24 | index]``: index = bits 0-23, category =
    bits 24-30, is_global = bit 31. rec_type 0x05 carries CATEGORY 1, which is a
    POSITIONAL index into that world's ``.PRS`` MESH TABLE; rec_type 0x04
    carries CATEGORY 2, an index into that world's ``.PRS`` ANIM TABLE. This
    walk parses all 359 blobs across all five worlds exactly, and in every world
    ``max_local_cat1 == mesh_count - 1``. Proof for the mesh half: Dark_Knight
    0x53's CASTLE cat-1 refs are 25, 25, 26, and CASTLE.PRS mesh[25] is the
    tag-6, 132,900-byte, 34-bone, 31-render-pass PSkeleton with textures exactly
    {33, 34} -- his mesh, identified independently.

    The mesh table is positional and per-world, so placing a foreign enemy means
    appending its referenced entries to the target ``.PRS`` and rewriting each
    LOCAL CATEGORY-1 reference to the new position, PRESERVING the flag byte.
    A GLOBAL reference (bit 31) is left strictly alone: that asset exists in
    every world already. The u16 at blob offset 22 remains discredited and is
    never read or written.

RENDER-PASS TEXTURE IDS ARE WORLD-LOCAL ``.PRT`` INDICES
    A mesh's render passes reference textures by DIRECT index into the world's
    OWN ``.PRT``, in file order. There is no global prefix in this field.

    This CORRECTS the research doc's Session 8 conclusion ("render-pass texture
    ids are ABSOLUTE cache indices; GLOBAL occupies 0..163 and world-local
    follows at ``global_tex_count + local``"). That model is DISPROVEN for this
    field, by measurement:

      * ``CASTLE.PRS`` mesh[10] and ``ICE.PRS`` mesh[18] are the SAME asset
        (identical 157,356-byte length, tag 6, 39 bones, 43 render passes) yet
        bind ids ``{9,10,11,12}`` in Castle and ``{21,22,23,24}`` in Ice.
        Extracting those eight ``.PRT`` entries shows they are pairwise
        BYTE-IDENTICAL -- same header fields (256x128 / 128x128 / 256x256 /
        256x256, format 0x13, size), same pixel bytes, same 1024-byte CLUTs.
        A global index would be the same number in both worlds; a world-local
        index is exactly what differs. The id indexes the world's own ``.PRT``.
      * Across all five worlds the maximum render-pass id found ANYWHERE is
        always below that world's own ``.PRT`` count_b and nowhere near 164:
        GRAVE max 34 / count_b 35, CASTLE 49 / 56, SWAMP 39 / 40, ICE 45 / 46,
        UNDER 41 / 46. Not one id in any world reaches its own count_b, and not
        one reaches GLOBAL.PRT's count of 164.

    Consequence for porting: EVERY render-pass texture id of a ported entry is
    SOURCE-WORLD-LOCAL and must be ported into the target ``.PRT`` and remapped
    to its new world-local slot. There is no "id below 164, leave it alone"
    case -- that branch is what made Dark_Knight's Castle ids 33/34 resolve to
    two unrelated GRAVE textures (GRAVE 33 is 128x128, CASTLE 33 is 256x128),
    i.e. the wrong-skin symptom. ``GLOBAL.PRT``'s count_b plays no part in this
    field's arithmetic; a new slot's index is simply the target ``.PRT``'s
    count_b immediately before the injection.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import (
    WALKING_MELEE_POOL,
    allowlist_lookup,
    is_native,
    name_for_type,
)

# Every message this subsystem emits carries this prefix. `ELF patch:` is
# reserved for executable patches and must NEVER appear on a BEF/PRS/PRT
# message.
LOG_PREFIX = "[cross-world]"


# --- FEATURE UNPARKED, HARD-GATED ------------------------------------------
# The .PRS mesh port path is ENABLED again, now driven by a blob's MATERIAL
# REFERENCES instead of the discredited u16@22. Every route still has to clear
# the gates in ``_port_enemy``:
#
#   * the blob's material section must walk exactly (record count and section
#     end both landing) -- ``bef.parse_material_refs`` raises otherwise;
#   * the TARGET .PRS mesh table must walk with ZERO resyncs and its walk end
#     must agree with the independent Anim-table anchor. This used to pass for
#     GRAVE alone, with CASTLE, SWAMP and UNDER landing 166-720 KB short of
#     their anchors -- which was NOT a property of those worlds but two wrong
#     field offsets in ``prs_mesh_parser`` (PSTAnim channel count u16@+0, not +2;
#     PVertexAnim element count u32@+4, not +0; see
#     research/PRS_MESH_TABLE_ENGINE_LAYOUTS.md). MEASURED NOW: all five worlds
#     walk their declared entry count with ZERO resyncs and land exactly on their
#     anchor, so all five PASS this gate. The gate is unchanged; only GRAVE is
#     actually targeted by the allowlist;
#   * every LOCAL CATEGORY-1 source entry must clear ``assess_source_entry``;
#   * no LOCAL CATEGORY-2 (anim) reference may point past the target world's
#     anim count -- see ``_check_local_anim_refs``.
#
# Any failure refuses the route and mutates nothing (all-or-nothing per enemy).
#
# HISTORICAL NOTE, kept because it cost a lot to learn:
# the premise the OLD path rested on was measured to be FALSE.
#
# ``mesh_port.MESH_INDEX_OFFSET = 22`` (a u16 in a BEF blob) was believed to be
# a positional index into the world's .PRS mesh table. It is not. Inside a
# single GRAVE.BEF, SEVEN native enemies all carry u16@22 = 25:
#   Zombie_Crocodile 0x0D, Axe_Skeleton 0x2F, Raven 0x37, Basic_Skeleton 0x47,
#   Guard_Skeleton 0x54, Torso_Zombie 0x6A, Ghost 0x6B
# Basic_Skeleton and Guard_Skeleton both render CORRECTLY in vanilla and look
# nothing alike, so 25 cannot be a shared model index. Three further natives
# share the value 30. The neighbouring u16@20 is not an entry tag either: it is
# 3 for 50 of 54 enemy entries and 6 for the other four, never 1.
#
# Consequence: every port this path performed extracted whatever object
# happened to occupy an unrelated index in a world mesh table, spliced it into
# the target .PRS, and rewrote a field that means something else entirely. The
# ports were byte-exact and the enemies did spawn -- wearing arbitrary props.
# GLOBAL.PRS is not the answer either: it holds tag-1 props at 25 and 30 and
# cannot even be walked (32 resyncs, the parse dies at entry 126 of 164 and
# finishes 965 KB short of its Anim-table anchor).
#
# Setting this back to False re-parks the whole path: ``port_enemy`` then
# refuses every route before reading a single source byte, ``iso_patcher``
# receives nothing, and the output is byte-identical to a cross-world-off run.
# It is a kill switch, not a tuning knob.
MESH_PORT_ENABLED = True

# --- HEURISTIC ANIMATION SUBSTITUTION --------------------------------------
# NOT A PORT. Anim entries cannot be extracted or inserted: a world's .PRS anim
# table holds records of MIXED types and only ~43% carry the ``TFA\xff`` magic
# (GRAVE declares 53 records, 23 signatures; CASTLE 109/38; SWAMP 78/23), the
# 0x0A u32 before each signature is a per-record TYPE TAG rather than a header
# constant, and no scannable marker exists for the other record types -- so
# entry boundaries are not derivable.
#
# What IS possible: point a ported enemy's LOCAL CATEGORY-2 references at the
# anim indices of a TARGET-WORLD NATIVE whose mesh has a similar bone count.
# Those indices are known to drive a real skeleton in that world already, so
# the enemy should move instead of sliding in its rest pose. The chosen
# animation is somebody else's, and a bone-count match is a proxy for skeleton
# compatibility, not a proof of it: the result may still be wrong. Every log
# line says so.
#
# With this False the module restores the previous behaviour exactly: in-range
# LOCAL CATEGORY-2 refs are left alone (and out-of-range ones still refuse the
# route). It is a switch, not a tuning knob -- no other logic reads it.
ANIM_SUBSTITUTION_ENABLED = True

# --- REAL ANIMATION PORTING (the enemy's OWN records) ----------------------
# This is a PORT, not the substitution above: the enemy's own anim records are
# appended to the target world's anim table and its LOCAL CATEGORY-2 references
# are repointed at the indices they land on. It is the path that CAN be correct,
# for a measured reason (`role-notes.md`, task 8.4): an SKA record addresses
# bones BY NAME, the four GRAVE donor natives use four mutually incompatible
# bone naming conventions, and so a BORROWED clip can never bind to a ported
# rig -- while an enemy's own clips name its own bones by construction.
#
# It is reachable ONLY where this switch is set AND ``anim_trust()`` passes for
# the target world (Requirement 9.3). Both halves matter: the switch is the
# human decision that the walker is trusted at all, the gate is the per-world
# evidence that this table can be walked, tiled and round-tripped.
#
# With it True and the gate passing for the target world, a refusal from the
# porter REFUSES THE ENEMY (Requirement 9.5). There is deliberately no fallback
# to the heuristic: falling back would place an enemy whose anim references were
# already rewritten by a path that then failed, which is precisely the
# partially-correct commit that desynchronises the engine's whole-table read.
#
# --- WHY THIS IS NOW True (task 11, the final checkpoint) ------------------
# It shipped False while the walker was unproven. It is True now because the
# evidence the switch was waiting for exists and was measured, not assumed:
#
#   * THE ANIM TRUST GATE PASSES FOR ALL FIVE WORLDS. ``assess_anim_table``
#     returns ok for GRAVE (53 records), CASTLE (109), SWAMP (78), ICE (82) and
#     UNDER (90): the walked count equals the declared count, the bounds tile
#     the region with no gap and no overlap, no walk event is raised, and every
#     record round-trips byte-identically. Nothing is scanned or resynced; the
#     cursor advances only by the ELF-derived per-tag ``record_length``.
#   * A REAL PORT IS PROVEN END TO END. Dark_Knight 0x53 on the actual
#     CASTLE->GRAVE route appends its OWN 11 anim records, taking GRAVE's
#     declared count 53 -> 64, with every appended record extracting back out
#     of the staged buffer byte-identically, the trailing bytes preserved, the
#     post-append re-walk clean, and ``verify_session`` passing every check.
#   * THE SUBSTITUTION HEURISTIC CANNOT BE CORRECT (role-notes.md, task 8.4).
#     An SKA record addresses bones BY NAME and the four GRAVE donor natives
#     use four mutually incompatible bone naming conventions, so a BORROWED
#     clip can never bind to a ported rig. An enemy's own clips name its own
#     bones by construction. Leaving the switch off keeps a path that is known
#     to be wrong; the reason this spec exists is that animations are wrong in
#     game, and a fix that is switched off cannot be confirmed in game.
#
# It REMAINS A KILL SWITCH. Setting it back to False restores the
# Substitution_Heuristic exactly: every enemy takes the heuristic branch
# including its out-of-range refusal, ``anim_trust()`` is never consulted, and
# not one anim byte is appended anywhere. Nothing else changed with the flip --
# no gate, refusal or heuristic code was relaxed to accommodate it.
#
# It is a kill switch, not a tuning knob -- no other logic reads it.
ANIM_PORT_ENABLED = True

# --- COMPANION ENTITY PORTING (the objects an enemy OWNS) ------------------
# Cross-world enemy porting itself now works in game: correct mesh, correct
# textures, and the enemy's OWN anim records appended by
# ``anim_port.port_anim_records``. A measured run placed 3 enemies with 0
# refusals, took GRAVE's declared anim count 53 -> 77 and its mesh count
# 39 -> 45, and ``verify_session`` passed all eight checks.
#
# What does NOT work is enemies that own ADDITIONAL OBJECTS. Bomb_Skeleton 0x49
# throws no bomb -- so it cannot damage the player at all -- and Dark_Knight
# 0x53 has no purple wall. That missing content is almost certainly not the
# enemy's own mesh entries: Bomb_Skeleton's CASTLE blob carries LOCAL
# CATEGORY-1 refs ``10 x8, 11 x2, 12 x1`` (three distinct indices, all ported)
# and Dark_Knight's are ``25, 25, 26`` (two distinct, both ported). The bomb and
# the wall are therefore believed to be SEPARATE entities that the enemy spawns
# at runtime.
#
# THE COMPANION RELATIONSHIP IS MEASURED. It was UNDER INVESTIGATION for a long
# time, and three methods failed: a separate-BEF-type-id pool (a pool, never an
# edge), a byte-value field scan inside the blob (a no-companion control enemy
# scored the same, and the only survivor was ``blob+0x10`` = the entry's own self
# id), and a level ``.PSX`` co-reference scan (a byte value that is not even a
# type id scored an order of magnitude higher). A FOURTH method did recover it:
# `randomizer/befscript.py` decodes every one of the game's 530 script sections
# at 100% coverage with 158 verified opcodes, and `49 CScriptApi::Spawn` takes
# the companion's BEF type id as a LITERAL push. Verdict
# `CANDIDATE_D_BYTECODE`; see
# ``.kiro/specs/companion-entity-porting/companion-notes.md``.
#
# --- WHY THIS IS NOW True (the completing pass) ----------------------------
# It shipped False while the edge was unproven and then while the SIZE CEILING
# was unbounded for a companion build. Both of those are now measured, not
# assumed:
#
#   * THE EDGE IS RECOVERED AND VERIFIED. `49 CScriptApi::Spawn`'s literal
#     operand, decoded with `befscript`'s 158-opcode ``verified`` map, names the
#     companion. All 11 allowlist routes were re-derived clean in one pass:
#     100% coverage, 0 refusals, 0 non-literal ``Spawn`` arguments, and the
#     seven previously measured sets all AGREE with the literals in
#     ``catalog.py``.
#   * THE BUILD IS MEASURED AND IT IS UNDER A PROVEN-GOOD FIGURE. The active set
#     built as ONE session (config A) measures **+1,538,265 .PRS / +565,584 .PRT
#     / +16,696 .BEF**. Both axes are UNDER the proven-good +1,987,430 /
#     +609,504, with .PRS 449,165 B under.
#   * IT IS CHEAPER THAN TODAY'S COMPANION-LESS BUILD. That build measures
#     +1,987,430 .PRS / +555,216 .PRT. Turning companions on makes 0x53 drop
#     out, and losing its +612,154 .PRS more than pays for the companions'
#     +162,989. So this does not approach the ceiling from either direction.
#
# THE TRADE. Dark_Knight 0x53 now REFUSES AS A WHOLE GROUP, so four enemies
# place instead of five. Its companion 0x8C needs ``CASTLE.PRS`` mesh[54], which
# sits after a TOP-LEVEL resync at entry 50, so ``assess_source_entry`` will not
# extract it. That is correct behaviour and nothing was relaxed to avoid it.
# Plant_Monster 0x08 would refuse for the same reason (its 0x0B needs mesh[49])
# if it were ever activated. Solving the ``prs_mesh_parser`` resync gap is the
# prerequisite for both.
#
# WHAT EACH ENEMY GAINS. Bomb_Skeleton 0x49 injects 0x5E -- the bomb, whose
# ``Spawn`` sits immediately after the throw sound effect -- plus 0x44 and 0xEB
# (the blast effects 0x5E itself spawns) and 0x63. Snowman 0x1D injects 0x1E,
# 0x9B and 0xBA (0x1E's mesh nodes are named ``snow_debris``,
# ``snow_debris#2..#8``). Zombie_Crocodile 0x0D and Crazed_Prisoner 0x58 are
# MEASURED to spawn nothing and are unaffected -- if either changes in game,
# something is wrong.
#
# THE SAFETY THAT MAKES THIS SURVIVABLE. Injection is APPEND-ONLY, via
# ``rebuild_with_injections(..., replace=False)``, so no progression anchor and
# no ``PROTECTED_TAGS`` entry can be touched. Each companion keeps its SOURCE
# type id, so the literal ``Spawn`` operand needs no rewrite. And
# ``CVmEntityManager::LoadBoundEntityFile`` aborts the load of the WHOLE .BEF if
# any single entry fails to parse -- which is why every staged blob is re-parsed
# before it is queued, and why anything unprovable is a refusal rather than a
# best effort.
#
# NOTHING HERE IS CONFIRMED. The bytecode proves WHAT each enemy spawns. It does
# not prove that 0x8C is the purple wall, or that 0x1E/0x9B/0xBA are the
# shockwave -- those were only ever named from mesh-node and call-site priors.
# Only the user seeing it in game settles that.
#
# TO REVERT: set this back to ``False``. One line, no other edit needed. With it
# False, every staged buffer is byte-identical to a run performed before this
# feature existed: no companion graph is walked, no companion blob is extracted,
# no BEF type id is allocated, and the CANDIDATE routes into GRAVE place exactly
# as they did. ``companion_memo`` and ``companion_blobs`` exist on every session
# but stay empty, and the three companion checks in ``verify_session`` iterate an
# empty list.
#
# It is a kill switch, not a tuning knob -- no other logic reads it. It is read
# at the companion entry point only: ``run_cross_world_placements`` chooses
# between ``_port_group_with_companions`` and the unchanged
# ``session.port_enemy``, and nothing else in the codebase mentions it.
COMPANION_PORT_ENABLED = True

# --- ONE-QUESTION EXPERIMENT HOOK (default OFF) ----------------------------
# This is NOT the companion feature and it is NOT a step towards it. It exists
# to answer ONE question that the Task 1 investigation left ``UNRESOLVED``:
#
#   is the "enemy X spawns object Y" reference made BY TYPE ID?
#
# If it is, then injecting a CASTLE-only entity into GRAVE under its ORIGINAL
# type id makes an unrewritten spawn reference resolve WITHOUT anyone having
# decoded the spawn instruction (design.md's Requirement 5.3 leverage). That is
# observable in game: port Bomb_Skeleton 0x49 into GRAVE as usual, additionally
# inject 0xEB (the mesh-adjacency BOMB PRIOR, and one of the 10 CASTLE-only ids
# no CASTLE level ever places) as a STANDALONE entity keeping id 0xEB, and look
# at whether the bomb appears. A bomb means the edge is by type id; no bomb
# means it is not, and the ``bef-script-vm-disassembler`` work is unavoidable.
#
# When non-empty, each listed type id is ported from
# ``COMPANION_EXPERIMENT_SOURCE_WORLD`` into ``COMPANION_EXPERIMENT_TARGET_WORLD``
# through the UNMODIFIED ``WorldPortSession.port_enemy`` path -- same trust
# gates, same mesh/texture/anim porters, same append-only BEF injection. No gate
# is relaxed for it: an id that refuses is refused, exactly like an allowlisted
# enemy. It bypasses ONLY ``classify_placement``, whose pool/allowlist tests are
# about ENEMY PLACEMENT and are meaningless for an entity nothing places.
#
# TO TURN IT OFF, set the tuple back to ``()``. That restores byte-identical
# output: no extra blob is read, no extra mesh/anim/texture is appended, and no
# BEF entry is injected.
#
# THE EXPERIMENT WAS RUN with ``(0xEB,)`` and is now SPENT. It ported cleanly
# (both trust gates PASS, CASTLE.PRS mesh[13] 21,820 B, 2 world-local textures,
# its own anim record appended, append-only BEF injection, ``verify_session``
# passing all eight checks) and the user tested the build in game: STILL NO
# BOMB, no freeze, no crash. So presence in the target world's .BEF under the
# ORIGINAL type id is NOT sufficient for the spawn to resolve -- the edge is not
# a plain type-id lookup satisfied by BEF presence, and Requirement 5.3's
# leverage does not work as a standalone fix. The null is also consistent with
# "0xEB simply is not the bomb" (it was only a mesh-adjacency prior); repeating
# with 0x4F or 0x63 would distinguish wrong-candidate from wrong-mechanism.
# See ``.kiro/specs/companion-entity-porting/companion-notes.md``.
#
# The hook is RETAINED, EMPTY, purely so that follow-up is one edit away. It is
# left empty because a non-empty tuple would silently inject an unrelated entity
# into every future GRAVE build.
#
# ``COMPANION_PORT_ENABLED`` above is NOT read by this hook, and the hook is not
# read by the companion path -- the two are independent. Nothing here walks a
# companion graph, allocates a type id, or resolves an edge; the real edge is now
# recovered from bytecode and handled by ``companion_port``, which is what the
# switch above turns on.
COMPANION_EXPERIMENT_TYPE_IDS: tuple[int, ...] = ()
COMPANION_EXPERIMENT_SOURCE_WORLD = "castle"
COMPANION_EXPERIMENT_TARGET_WORLD = "grave"

_PARKED_REASON = (
    "the .PRS mesh port path is disabled (MESH_PORT_ENABLED=False)"
)

_PARKED_LOG_BLOCK = (
    "=" * 72,
    "CROSS-WORLD ENEMY PORTING IS DISABLED (MESH_PORT_ENABLED=False).",
    "EFFECT: NO enemies will be ported. Every non-native route is refused",
    "     before any mesh extraction, and no .PRS, .PRT or .BEF is emitted --",
    "     the patched output is byte-identical to a cross-world-off run.",
    "=" * 72,
)


# --- Path outcome constants -------------------------------------------------
NATIVE = "NATIVE"                              # mesh already in target .PRS
PORT = "PORT"                                  # mesh + textures must be ported
FALLBACK = "FALLBACK"                          # port failed; place anyway
SKIP_TOGGLE_OFF = "SKIP_TOGGLE_OFF"            # non-native, cross-world off
SKIP_NOT_ALLOWLISTED = "SKIP_NOT_ALLOWLISTED"  # non-native, route unverified
REFUSE_NOT_MELEE = "REFUSE_NOT_MELEE"          # not a walking-melee enemy
# The trustworthiness gate refused the route: something about the parse of the
# source entry, the target mesh table, or the entry's own contents means the
# bytes we would splice in cannot be trusted. Nothing is placed, nothing is
# mutated. See WorldPortSession._gate_* and mesh_port's trust helpers.
REFUSE_UNTRUSTWORTHY = "REFUSE_UNTRUSTWORTHY"


@dataclass
class PlacementPlan:
    """One classified placement. Produced only by ``classify_placement``."""

    enemy_id: int
    name: str
    target_world: str
    path: str
    source_world: str | None = None
    status: str | None = None
    reason: str = ""


@dataclass
class PortResult:
    """Outcome of porting one enemy's resources into a target world."""

    plan: PlacementPlan
    ok: bool
    mesh_index: int | None
    tex_remap: dict[int, int] = field(default_factory=dict)
    ported_textures: int = 0
    fallback: bool = False
    warnings: list[str] = field(default_factory=list)
    # True only when a mesh entry was actually APPENDED to the session's .PRS
    # for this enemy. False for a no-mesh fallback (blob keeps its original
    # index) and for a malformed-plan failure. Verification needs this fact
    # exactly, and it cannot be inferred reliably from ``mesh_index`` alone:
    # a no-mesh fallback records the blob's SOURCE-world index, which may
    # coincidentally be >= the target world's pristine mesh count.
    mesh_committed: bool = False
    # True when the trustworthiness gate refused this route. A refusal places
    # NOTHING and mutates no buffer -- it is not a fallback.
    refused: bool = False
    # source mesh-table index -> index it now occupies in the target .PRS.
    # DEDUPED: an enemy referencing the same source index several times (
    # Dark_Knight 0x53 references 25 twice, Bomb_Skeleton 0x49 references 10
    # eight times) ports it ONCE and every reference is rewritten to the one
    # new index, so this dict has one entry per DISTINCT source index.
    mesh_indices: dict[int, int] = field(default_factory=dict)
    # How many mesh entries this port actually appended to the target .PRS ==
    # len(mesh_indices) minus any already ported earlier in the session.
    ported_meshes: int = 0
    # Per rewritten material ref: blob offset of the index field -> (flag byte
    # that MUST be unchanged, index it was rewritten to). Verification asserts
    # both against the rebuilt blob.
    ref_rewrites: dict[int, tuple[int, int]] = field(default_factory=dict)
    # The blob exactly as it arrived, so verification can prove every GLOBAL
    # ref (and every non-ref byte) was left untouched. BEF blobs are a few KB.
    original_blob: bytes | None = None
    #: Signature ``(length, tag, bone_count, render_pass_count)`` per ported
    #: source index -- the stable cross-world identity key.
    signatures: dict[int, tuple] = field(default_factory=dict)
    # Per HEURISTICALLY SUBSTITUTED anim ref: blob offset of the index field ->
    # (flag byte that MUST be unchanged, index it was rewritten to). Empty when
    # ANIM_SUBSTITUTION_ENABLED is False or no donor could be chosen.
    anim_rewrites: dict[int, tuple[int, int]] = field(default_factory=dict)
    #: The donor native whose anim indices were borrowed, or None.
    anim_donor: "AnimDonor | None" = None
    # --- the PORT branch (ANIM_PORT_ENABLED), kept STRICTLY SEPARATE from the
    # heuristic fields above so verification can tell which path wrote a ref.
    # Per PORTED anim ref: blob offset of the index field -> (flag byte that
    # MUST be unchanged, index it was rewritten to). Empty on the heuristic
    # branch. ``anim_rewrites`` and ``anim_port_rewrites`` are never both
    # populated: exactly one branch runs per enemy.
    anim_port_rewrites: dict[int, tuple[int, int]] = field(default_factory=dict)
    #: source anim index -> index the ported record now occupies in the target
    #: table (includes indices supplied by the session dedupe memo).
    ported_anim_indices: dict[int, int] = field(default_factory=dict)
    #: How many anim records this port APPENDED to the target .PRS (0 when a
    #: record was already ported earlier in the session, or on the heuristic
    #: branch).
    appended_anims: int = 0
    #: Bone count of the ported enemy's skeletal mesh (max across its entries).
    bone_count: int | None = None
    # --- COMPANION GROUP (Requirement 9.4) --------------------------------
    #: How many Companion_Entities this route carried. Always 0 while
    #: ``COMPANION_PORT_ENABLED`` is False, because nothing resolves a group.
    companion_count: int = 0
    #: Their SOURCE type ids, in the order the group resolved them.
    companion_type_ids: tuple = ()
    #: How many of them needed a BEF injection. A companion already present in
    #: the target world -- 0x3A's 0x35 is the measured case -- reuses the
    #: existing entry and injects nothing, so this can be lower than
    #: ``companion_count``.
    companion_injected: int = 0


@dataclass
class AnimDonor:
    """A target-world NATIVE enemy whose anim indices are borrowed wholesale."""

    enemy_id: int
    name: str
    bone_count: int
    anim_indices: list[int]
    mesh_indices: list[int]

    def describe(self) -> str:
        return (f"{self.name} 0x{self.enemy_id:02X} ({self.bone_count} bones, "
                f"mesh {self.mesh_indices}, anim {self.anim_indices})")


def select_anim_donor(bef_bytes: bytes, prs_bytes: bytes, world: str,
                      bone_count: int, log=None, *,
                      needed_slots: int | None = None):
    """Pick a target-world native that can COVER the ported enemy's anim slots.

    REASONING, in full, because this is the auditable heart of the heuristic:

    A ported enemy T-poses because its LOCAL CATEGORY-2 (anim) references name
    anim-table slots of ITS OWN world, and in the target world those slots hold
    either an unrelated animation or an animation authored against a different
    bone hierarchy, so nothing binds and the skeleton stays in its rest pose.
    Anim entries cannot be ported (boundaries are not derivable -- see
    ``ANIM_SUBSTITUTION_ENABLED``), so the only lever left is to point the
    ported enemy at anim slots that ALREADY drive a humanoid skeleton in the
    target world: those of the world's own natives.

    Which native? Bone count alone was tried first and was inadequate: it
    measures skeleton size but says nothing about how many clips the donor owns
    or what they mean. GRAVE's Ghost, a 3-slot hovering floater, kept winning
    for a 34-bone walking knight on bone proximity alone, which collapsed 9 of
    the knight's 11 anims onto a single hover clip -- every ref resolved, and
    the enemy still looked broken.

    So COVERAGE is now the primary key. When ``needed_slots`` is given (the
    number of DISTINCT local category-2 anim indices the ported enemy names),
    each candidate's shortfall is ``max(0, needed_slots - len(anim_indices))``
    and the lowest shortfall wins: a donor that can give every needed slot its
    own clip beats a donor with a closer bone count. Bone proximity is the
    TIEBREAK among equally-covering donors, then the lower enemy id purely for
    determinism. With ``needed_slots=None`` every shortfall is 0 and the legacy
    bone-only ordering applies unchanged.

    Bone count is still the only skeleton property measurable on both sides
    without parsing the skeleton itself, and an animation targets a specific
    joint list, so a similar joint count remains the best available proxy for
    "these two skeletons are close enough for the channels to land". It is a
    PROXY. Equal bone counts do not prove equal hierarchies or equal bone
    order, so the substituted animation may still play wrongly. This is a
    HEURISTIC SUBSTITUTION, NOT A PORT.

    Donor eligibility, all required:
      * present in the target world's own ``.BEF`` and confirmed native by
        ``catalog.is_native`` (a blob merely present is not proof of nativity);
      * in ``WALKING_MELEE_POOL`` -- we want a walker's locomotion set;
      * its material section walks (otherwise nothing about it is trustworthy);
      * it has at least one LOCAL CATEGORY-1 ref, so a bone count is measurable,
        and at least one LOCAL CATEGORY-2 ref, so there is something to borrow.
        A native whose refs are all GLOBAL (GRAVE's Basic_Skeleton, SwordSkeleton,
        Guard_Skeleton, Basic_Zombie) is unusable as a donor here: its animation
        lives in ``GLOBAL.PRS``, not in this world's anim table, so its indices
        would resolve against the wrong file.

    Bone count of a candidate is the MAXIMUM across its local cat-1 meshes: an
    enemy commonly names one skinned skeleton plus 0-bone props, and the
    skeleton is what an animation drives.

    Returns an ``AnimDonor`` or ``None`` when no native qualifies.
    """
    from .bef import (
        MATERIAL_CATEGORY_ANIM,
        MATERIAL_CATEGORY_MESH,
        BefFile,
        MaterialSectionError,
        parse_material_refs,
    )
    from .mesh_port import mesh_entry_signature

    emit = log if log is not None else (lambda _m: None)
    try:
        target_bef = BefFile.parse(bef_bytes)
    except Exception as exc:  # noqa: BLE001 - no donor is a valid answer
        emit(f"anim donor search: {world.upper()}.BEF does not parse "
             f"({type(exc).__name__}: {exc}); no substitution possible")
        return None

    candidates: list[AnimDonor] = []
    for tid in sorted(WALKING_MELEE_POOL):
        if not target_bef.has_type(tid) or not is_native(tid, world):
            continue
        try:
            refs = parse_material_refs(target_bef.get_entry(tid).data)
        except MaterialSectionError:
            continue
        meshes = sorted({r.index for r in refs
                         if not r.is_global and r.category == MATERIAL_CATEGORY_MESH})
        anims = sorted({r.index for r in refs
                        if not r.is_global and r.category == MATERIAL_CATEGORY_ANIM})
        if not meshes or not anims:
            continue
        bones = []
        for index in meshes:
            try:
                bones.append(mesh_entry_signature(prs_bytes, index)[2])
            except Exception:  # noqa: BLE001 - unmeasurable mesh, skip it
                continue
        if not bones:
            continue
        candidates.append(AnimDonor(enemy_id=tid, name=name_for_type(tid),
                                    bone_count=max(bones),
                                    anim_indices=anims, mesh_indices=meshes))

    if not candidates:
        emit(f"anim donor search: {world.upper()} has no native with both a "
             f"local mesh ref and a local anim ref; no substitution possible")
        return None

    def shortfall(cand: AnimDonor) -> int:
        if needed_slots is None:
            return 0
        return max(0, needed_slots - len(cand.anim_indices))

    for cand in candidates:
        emit(f"anim donor candidate {cand.describe()} "
             f"|{cand.bone_count} - {bone_count}| = "
             f"{abs(cand.bone_count - bone_count)}, "
             f"{len(cand.anim_indices)} anim slot(s), "
             f"shortfall {shortfall(cand)}"
             + (f" of {needed_slots} needed" if needed_slots is not None else ""))
    best = min(candidates,
               key=lambda c: (shortfall(c), abs(c.bone_count - bone_count),
                              c.enemy_id))
    best_shortfall = shortfall(best)
    emit(f"anim donor chosen {best.describe()} shortfall {best_shortfall}")
    if best_shortfall > 0:
        emit(f"anim donor COLLAPSE: {best.name} 0x{best.enemy_id:02X} owns "
             f"{len(best.anim_indices)} anim slot(s) for {needed_slots} needed, "
             f"so {best_shortfall + 1} source anims will collapse onto its last "
             f"index {best.anim_indices[-1]}")
    return best


class _PortRefusal(Exception):
    """Internal: a per-entry step failed, so the WHOLE enemy must be refused.

    Raised by ``WorldPortSession._port_one_mesh`` and caught by
    ``_port_enemy``, which turns it into a ``refuse``. It never escapes the
    module: it exists so a failure deep inside one of several mesh entries
    unwinds without half-applying the enemy's other entries.
    """


class GlobalTexCountError(Exception):
    """``GLOBAL.PRT``'s texture count could not be established.

    Run-fatal for cross-world placements. Never downgraded to a default.
    """


def classify_placement(
    enemy_id: int,
    target_world: str,
    *,
    cross_world: bool,
    allowlist: dict | None = None,
) -> PlacementPlan:
    """Classify one candidate placement into exactly one outcome path.

    Precedence, exactly as in design.md's decision table:

      1. not in ``WALKING_MELEE_POOL``     -> ``REFUSE_NOT_MELEE``
      2. native to ``target_world``        -> ``NATIVE``
      3. ``cross_world`` off               -> ``SKIP_TOGGLE_OFF``
      4. allowlisted for this route        -> ``PORT``
      5. otherwise                         -> ``SKIP_NOT_ALLOWLISTED``

    Two ordering subtleties are deliberate. The not-melee refusal comes FIRST,
    so allowlist contents and toggle state can never admit a non-melee enemy.
    The native check comes BEFORE the toggle check, because a native enemy is
    placeable whether or not cross-world is enabled -- placing it needs no
    resource port at all.

    ``REFUSE_UNTRUSTWORTHY`` is NOT produced here. It is an outcome
    *discovered during porting*: anything the gates cannot vouch for (a
    material section that does not walk, a dirty target-table walk, a broken
    source index mapping, a slice that does not round-trip, a texture port that
    raised, a local anim reference past the target's anim count) turns a
    ``PORT`` plan into ``REFUSE_UNTRUSTWORTHY``. Classification itself is static
    and happens before any file is touched.

    ``FALLBACK`` is RETIRED. Every former downgrade -- including the
    texture-only one -- is now a refusal, because a partially-correct commit can
    desynchronise the engine's sequential read of the whole target mesh table.
    The constant remains only so old logs and callers still resolve.
    """
    name = name_for_type(enemy_id)

    if enemy_id not in WALKING_MELEE_POOL:
        return PlacementPlan(
            enemy_id, name, target_world, REFUSE_NOT_MELEE,
            reason=(f"{name} 0x{enemy_id:02X} is not a walking-melee enemy; "
                    f"refusing placement into {target_world}"),
        )

    if is_native(enemy_id, target_world):
        return PlacementPlan(enemy_id, name, target_world, NATIVE)

    if not cross_world:
        return PlacementPlan(
            enemy_id, name, target_world, SKIP_TOGGLE_OFF,
            reason=(f"{name} 0x{enemy_id:02X} is not native to {target_world} "
                    f"and cross-world spawning is disabled"),
        )

    entry = allowlist_lookup(enemy_id, target_world, allowlist)
    if entry is not None:
        return PlacementPlan(
            enemy_id, name, target_world, PORT,
            source_world=entry.source_world, status=entry.status,
        )

    return PlacementPlan(
        enemy_id, name, target_world, SKIP_NOT_ALLOWLISTED,
        reason=(f"{name} 0x{enemy_id:02X} -> {target_world}: not in "
                f"Cross_World_Allowlist"),
    )


# .PRT header: u32 magic at 0x00 (bytes 01 02 03 04), counts at 0x08/0x0C,
# 32-byte header total.
_PRT_MAGIC = 0x04030201
_PRT_HEADER_SIZE = 32
_TEX_COUNT_OFFSET = 0x0C
# Sanity ceiling. GLOBAL.PRT holds on the order of a couple hundred textures;
# the whole shared cache is bounded by PS2 VRAM budgets, so anything past a few
# thousand is a mis-parse, not a real count. 65535 is a generous ceiling that
# still catches a garbage u32 read (which would be in the millions/billions).
_MAX_PLAUSIBLE_TEX_COUNT = 65535


def read_global_tex_count(global_prt_path) -> int:
    """Read ``count_b`` (u32 at offset 0x0C) from ``GLOBAL.PRT``'s header.

    DIAGNOSTIC ONLY -- NOT part of the port path any more. This returns how
    many textures ``GLOBAL.PRT`` declares (164 in retail). It was once believed
    to be a shared-texture-cache boundary that render-pass ids were measured
    against (``count + local_index``); that model is DISPROVEN for render-pass
    ids -- see the module docstring for the CASTLE.PRT 9-12 / ICE.PRT 21-24
    byte-identity evidence and the per-world maxima. Nothing in ``.PRS``
    render-pass id arithmetic uses this number; do not reintroduce it there.

    Raises ``GlobalTexCountError`` if the file is missing, is shorter than the
    32-byte header, has a wrong magic, or reports an implausible count.

    NEVER returns a fabricated or defaulted count. ``bef.inject_locked_enemy``
    contains a hardcoded ``global_tex_count = 164`` fallback; that pattern is
    deliberately NOT replicated here. A wrong boundary silently mis-points
    every ported texture in the run -- every render pass resolves to an
    unrelated texture, with no error anywhere -- which is strictly worse than
    not porting at all. Failing loudly lets the caller abort cross-world
    placements with a logged reason.

    Accepts a ``str`` or ``Path``.
    """
    path = Path(global_prt_path)
    try:
        with path.open("rb") as fh:
            header = fh.read(_PRT_HEADER_SIZE)
    except FileNotFoundError as exc:
        raise GlobalTexCountError(
            f"{LOG_PREFIX} GLOBAL.PRT not found at {path}"
        ) from exc
    except OSError as exc:
        raise GlobalTexCountError(
            f"{LOG_PREFIX} GLOBAL.PRT at {path} could not be read: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if len(header) < _PRT_HEADER_SIZE:
        raise GlobalTexCountError(
            f"{LOG_PREFIX} GLOBAL.PRT at {path} is {len(header)} bytes; "
            f"a .PRT header is {_PRT_HEADER_SIZE} bytes"
        )

    magic = struct.unpack_from("<I", header, 0)[0]
    if magic != _PRT_MAGIC:
        raise GlobalTexCountError(
            f"{LOG_PREFIX} GLOBAL.PRT at {path} has bad magic "
            f"0x{magic:08X}, expected 0x{_PRT_MAGIC:08X}"
        )

    count = struct.unpack_from("<I", header, _TEX_COUNT_OFFSET)[0]
    if count <= 0 or count > _MAX_PLAUSIBLE_TEX_COUNT:
        raise GlobalTexCountError(
            f"{LOG_PREFIX} GLOBAL.PRT at {path} reports an implausible "
            f"texture count {count} (count_b@0x{_TEX_COUNT_OFFSET:02X})"
        )
    return count


# --- Porting session -------------------------------------------------------

_MESH_COUNT_OFFSET = 0x20
_PRT_CLUT_COUNT_OFFSET = 0x08


def _default_log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}")


class WorldPortSession:
    """Owns one target world's in-memory ``.PRS`` / ``.PRT`` / ``.BEF`` buffers
    for the whole run.

    The three target files are read from disk EXACTLY ONCE, here in
    ``__init__``. Every port mutates the buffers this object holds, and a
    pristine copy of each is retained alongside so verification can diff
    against the original without touching disk again.

    WHY THIS CLASS EXISTS
        ``bef.inject_locked_enemy`` re-read the target ``.PRS``, ``.PRT`` and
        ``.BEF`` from disk on EVERY call and returned fresh buffers built from
        those pristine bytes. Porting two enemies into one world therefore
        produced two independent single-enemy results, and whichever was
        written last silently discarded the other -- the first enemy's mesh,
        its textures and its BEF entry all vanished, with no error anywhere.
        Threading one set of buffers through every port is the entire point of
        this class, and it is what makes ``local_tex_count`` (and hence
        texture cache index assignment) correct across a batch.

    Nothing here writes to disk. ``finish()`` returns buffers; the caller hands
    them to ``iso_patcher.write_patched_assets``, which is the only writer.
    """

    def __init__(self, target_world: str, game_files_dir,
                 global_tex_count: int | None = None, log=None,
                 *, archive_stem: str | None = None) -> None:
        self.log = log if log is not None else _default_log
        self.target_world = target_world
        self.game_files_dir = Path(game_files_dir)
        # NOT used by the port path any more, and NOT required: render-pass
        # texture ids are world-local ``.PRT`` indices (see the module
        # docstring), so GLOBAL.PRT's count_b never enters the arithmetic.
        # Retained only as an informational field for callers that still log
        # it; ``None`` is a perfectly valid value.
        self.global_tex_count = (None if global_tex_count is None
                                 else int(global_tex_count))

        # WHICH ARCHIVE SET THIS SESSION OWNS. Normally the target WORLD's own
        # trio (`GRAVE.PRS` / `.PRT` / `.BEF`), which is what `target_world`
        # spells. `archive_stem` is the ONE opt-in override: a BOSS ARENA ships
        # its own separate trio (`GRAVE_B.*`, `SWAMP_B.*`, `ICE_B.*`,
        # `UNDER_B.*`) and `boss_arena` opens a session on it so an arrival can
        # reach the arena EXACTLY the way it reaches a world -- same buffers,
        # same trust gates, same porter, same `verify_session`. Nothing else
        # changes: `target_world` stays the lowercase WORLD name, so
        # `classify_placement`, `allowlist_lookup`, `route_companions` and the
        # cost tables are all keyed exactly as they were, and `_source_path`
        # still resolves a SOURCE world's files by world name.
        self.archive_stem = str(archive_stem or target_world).upper()
        upper = self.archive_stem
        self._prs_name = f"{upper}.PRS"
        self._prt_name = f"{upper}.PRT"
        self._bef_name = f"{upper}.BEF"

        # The one and only read of the target world's files.
        self.prs = (self.game_files_dir / self._prs_name).read_bytes()
        self.prt = (self.game_files_dir / self._prt_name).read_bytes()
        self.bef = (self.game_files_dir / self._bef_name).read_bytes()
        self.pristine_prs = self.prs
        self.pristine_prt = self.prt
        self.pristine_bef = self.bef

        # count_b at 0x0C: how many textures the target world already owns.
        # Incremented once per injection so a batch gets consecutive cache
        # indices with no gap and no collision.
        self.local_tex_count = struct.unpack_from("<I", self.prt, _TEX_COUNT_OFFSET)[0]
        self._initial_local_tex_count = self.local_tex_count

        self.results: list[PortResult] = []
        self.pending_blobs: list = []          # (type_id, patched_blob) to inject
        # (source_world, source_local_index) -> new cache index. A texture
        # already ported earlier in this run is reused, never injected twice.
        self.tex_memo: dict[tuple[str, int], int] = {}
        # (source_world, category, source_index) -> index the ported entry now
        # occupies in this session's .PRS. The DEDUPE memo: a source entry
        # referenced by several refs, or by several enemies, is ported ONCE and
        # every reference is pointed at the same new index.
        self.mesh_memo: dict[tuple[str, int, int], int] = {}
        # (source_world, category, source_index) -> mesh signature, so a
        # deduped (already-ported) entry can still supply its bone count to the
        # anim-donor heuristic without re-reading the source .PRS.
        self.sig_memo: dict[tuple[str, int, int], tuple] = {}
        # (source_world, 2, source_anim_index) -> index the ported anim record
        # now occupies in this session's .PRS anim table. Same shape and same
        # purpose as ``mesh_memo``: a record wanted by several refs, or by
        # several enemies, is appended ONCE (Requirement 8.8). Written ONLY
        # alongside adopting a staged .PRS, never before.
        self.anim_memo: dict[tuple[str, int, int], int] = {}
        # (source_world, source_type_id) -> companion_port.CompanionRecord, the
        # SESSION-LEVEL COMPANION MEMO (Requirements 7.1-7.4), in the same style
        # as ``mesh_memo`` / ``anim_memo``: a companion two root enemies share is
        # ported ONCE and the second group reuses the recorded target mesh, anim
        # and texture indices. Written only alongside a COMMITTED group, so a
        # refused group can never leave an entry naming resources that were
        # rolled back.
        self.companion_memo: dict[tuple[str, int], object] = {}
        # (target_type_id, blob, source_world, source_type_id) queued for
        # APPEND-ONLY injection by ``finish()``. Separate from
        # ``pending_blobs`` on purpose: a pending blob is an ENEMY with a
        # ``PortResult`` behind it, and ``verify_session``'s blob check keys on
        # exactly that. A companion has no placement and no PortResult, and is
        # verified by its own three checks instead.
        self.companion_blobs: list = []
        # bone_count -> AnimDonor|None. The donor search walks the target .BEF
        # and signatures several of its meshes, so it is done once per distinct
        # bone count per session.
        self._anim_donors: dict[tuple[int, int | None],
                                "AnimDonor | None"] = {}

        self.log(f"{upper}: session opened (PRS {len(self.prs)} B, "
                 f"PRT {len(self.prt)} B, BEF {len(self.bef)} B), "
                 f"local textures={self.local_tex_count}")

    # -- texture index arithmetic -----------------------------------------
    def assign_tex_index(self) -> int:
        """Reserve the next WORLD-LOCAL ``.PRT`` index for a texture injected
        into the target world: the target ``.PRT``'s ``count_b`` immediately
        BEFORE this injection, i.e. ``local_tex_count``, then bump it.
        Consecutive calls yield consecutive indices.

        NO global prefix is added. The old ``global_tex_count + local_tex_count``
        arithmetic came from the disproven absolute-cache model (see the module
        docstring: CASTLE.PRT 9-12 and ICE.PRT 21-24 are byte-identical
        textures reached by DIFFERENT ids from the same asset), and it pushed
        every ported id ~164 slots past anything the target world owns.
        """
        new_id = self.local_tex_count
        self.local_tex_count += 1
        return new_id

    # -- helpers -----------------------------------------------------------
    def _source_path(self, source_world: str, ext: str) -> Path:
        return self.game_files_dir / f"{source_world.upper()}.{ext}"

    def mesh_count(self) -> int:
        return struct.unpack_from("<I", self.prs, _MESH_COUNT_OFFSET)[0]

    def target_anim_count(self):
        """The target world's Anim-table entry count, from the LIVE buffer.

        Read from ``self.prs`` -- the buffer the engine will actually read --
        and memoised until ``refresh_anim_count`` clears it. It USED to read
        ``pristine_prs`` on the grounds that "porting never adds an anim
        entry"; ``anim_port.append_anim_records`` makes that false, and a
        cached pristine figure would then be a bound that is too LOW, refusing
        legitimately valid CATEGORY-2 refs for the second enemy of a session
        while disagreeing with the buffer that ships.

        While nothing appends, the live and pristine counts are identical (a
        mesh insert shifts the anim table but does not change its count word),
        so this returns exactly what it returned before -- the substitution
        path is unaffected.

        ``None`` means the header could not be read, which is a refusal
        condition: without it there is no way to bound a CATEGORY-2 reference.
        """
        cached = getattr(self, "_target_anim_count", "unset")
        if cached == "unset":
            from .mesh_port import read_anim_count
            cached = read_anim_count(self.prs)
            self._target_anim_count = cached
            self.log(f"{self.target_world.upper()}: anim table declares "
                     f"{cached} entr{'y' if cached == 1 else 'ies'}")
        return cached

    def refresh_anim_count(self, expected=None):
        """Drop the cached anim count and re-read it from the LIVE buffer.

        THE INVALIDATION HOOK. Call it immediately after adopting a staged
        ``.PRS`` that had anim records appended to it (the task 10.1 route);
        never rely on an incidental side effect somewhere else to do it.

        ``expected``, when given, is the count the append REPORTED
        (``AnimPortOutcome.new_count``). A disagreement between that and the
        count word actually in the buffer means the append and the buffer have
        desynchronised, which is precisely the condition that makes the engine's
        sequential read of the whole table run off the rails -- so it is logged
        loudly. Returns the fresh count.
        """
        self._target_anim_count = "unset"
        fresh = self.target_anim_count()
        if expected is not None and fresh != expected:
            self.log(f"WARNING {self.target_world.upper()}: anim count "
                     f"re-read from the live buffer is {fresh} but the append "
                     f"reported {expected}; the table's count word and its "
                     f"record chain disagree")
        return fresh

    def locate_anim_region(self, staged_prs=None):
        """Locate the anim region in ``staged_prs`` (default: the live buffer).

        NEVER CACHED, on purpose. ``mesh_port.find_anim_table_anchor`` derives
        the anchor from the first ``TFA\\xff`` offset, which sits AFTER the mesh
        table, so ``insert_mesh_entry`` MOVES it -- every anim offset, anchor and
        region taken before a mesh insert is wrong after it. The ordering rule
        this enforces: within one enemy's port, mesh entries are inserted FIRST
        and the anim region is re-located from the STAGED buffer afterwards.

        Returns an ``anim_port.AnimRegion`` or ``None``.
        """
        from .anim_port import find_anim_region
        return find_anim_region(self.prs if staged_prs is None else staged_prs)

    def _fail(self, plan: PlacementPlan, message: str) -> PortResult:
        """Record a recoverable per-enemy failure. Mutates no buffer.

        Used only for a MALFORMED PLAN (no source world), where there is
        nothing to port and nothing to refuse a route for. Every other port
        failure goes to ``refuse``.
        """
        self.log(f"WARNING {plan.name} 0x{plan.enemy_id:02X} "
                 f"{plan.source_world}->{self.target_world}: {message}")
        result = PortResult(plan=plan, ok=False, mesh_index=None,
                            warnings=[message])
        self.results.append(result)
        return result

    def _rollback_reserved(self, reserved) -> None:
        """Give back the cache indices reserved for a port that then failed, and
        forget their memo entries, so a later successful port gets the next
        index with no gap and no stale reuse of an index whose texture was never
        committed into ``self.prt``."""
        if not reserved:
            return
        self.local_tex_count -= len(reserved)
        dead = set(reserved)
        for key in [k for k, v in self.tex_memo.items() if v in dead]:
            del self.tex_memo[key]

    def _note_no_anim(self, plan: PlacementPlan) -> None:
        """Requirement 6.5: emitted once per SUCCESSFUL cross-world placement,
        fallbacks included. The Anim table format is unsolved, so animation and
        sound data are never ported -- a known, user-accepted limitation."""
        self.log(f"NOTE {plan.name} 0x{plan.enemy_id:02X}: animation and sound "
                 f"data were not ported")

    def refuse(self, plan: PlacementPlan, reason: str,
               details=()) -> tuple[bytes | None, PortResult]:
        """REFUSE a route outright. Places nothing, mutates no buffer.

        This replaces every former "port the mesh anyway" downgrade except the
        genuinely texture-only one. Committing an entry whose byte range or
        index mapping is not provably correct desynchronises the engine's
        sequential read of the WHOLE target mesh table, not just this enemy --
        the observed failure mode is a hard freeze on level load with a storm of
        TLB misses as the engine follows garbage read out of the wrong offsets.
        No enemy is worth that, so a route that cannot be trusted is dropped
        loudly instead.
        """
        plan.path = REFUSE_UNTRUSTWORTHY
        plan.reason = reason
        self.log(f"REFUSE {plan.name} 0x{plan.enemy_id:02X} "
                 f"{str(plan.source_world).upper()}->"
                 f"{self.target_world.upper()}: {reason}")
        for line in details:
            self.log(f"  detail {line}")
        result = PortResult(plan=plan, ok=False, mesh_index=None,
                            refused=True, warnings=[reason])
        self.results.append(result)
        return None, result

    # -- trustworthiness gate ---------------------------------------------
    def target_trust(self):
        """Gate the target world's mesh table ONCE per session and cache it.

        Assessed against the PRISTINE buffer: every committed port appends a
        trusted entry, so re-walking the growing buffer would only re-verify
        what this already established while costing a full multi-megabyte walk
        per enemy.
        """
        cached = getattr(self, "_target_trust", None)
        if cached is None:
            from .mesh_port import assess_target_table
            cached = assess_target_table(self.pristine_prs,
                                         label=f"{self.target_world.upper()}.PRS")
            self._target_trust = cached
            for line in cached.details:
                self.log(f"gate {line}")
            if cached.ok:
                self.log(f"gate PASS {self._prs_name}: mesh-table walk is "
                         f"clean and its end agrees with the independent "
                         f"Anim-table anchor")
            else:
                self.log(f"gate FAIL {self._prs_name}: {cached.reason}")
        return cached

    def anim_trust(self):
        """Gate the target world's ANIM table ONCE per session and cache it.

        Assessed against the PRISTINE buffer for the same reason
        ``target_trust`` is: every committed port appends trusted bytes, so
        re-walking the growing buffer would only re-verify what the first
        assessment established while costing a full multi-megabyte walk per
        enemy (Requirement 5.4).

        DECIDED, and deliberately NOT changed alongside ``target_anim_count``:
        this stays PRISTINE-BASED. The two are different kinds of fact. The
        count is a BOUND that every rewritten index is checked against, so it
        must describe the buffer that ships. The trust gate is a PROPERTY OF THE
        WORLD AS SHIPPED -- "can this table be walked, tiled and round-tripped
        at all?" -- and the only bytes a port adds to it are records the porter
        already gated and re-walked (``port_anim_records`` step 8 re-asserts
        ``assess_anim_table`` on its staged buffer, so the post-condition is
        checked there, per-append, where a failure can still refuse). Making
        this live-based would re-walk a multi-megabyte table once per enemy to
        re-verify what the porter just verified (Requirement 5.4 exists to
        forbid exactly that).

        Logged through ``self.log``, which already carries the ``[cross-world]``
        prefix; the ``ELF patch:`` prefix is reserved for executable patches and
        never appears on an asset message (Requirements 5.5, 10.6).
        """
        cached = getattr(self, "_anim_trust", None)
        if cached is None:
            from .anim_port import assess_anim_table
            cached = assess_anim_table(self.pristine_prs,
                                       label=f"{self.target_world.upper()}.PRS")
            self._anim_trust = cached
            for line in cached.details:
                self.log(f"anim gate {line}")
            if cached.ok:
                self.log(f"anim gate PASS {self._prs_name}: anim-table walk "
                         f"tiles the region, reproduces the declared count and "
                         f"every record round-trips byte-identically")
            else:
                self.log(f"anim gate FAIL {self._prs_name}: {cached.reason}")
        return cached

    # -- the port ----------------------------------------------------------
    def port_enemy(self, plan: PlacementPlan, blob: bytes) -> tuple[bytes, PortResult]:
        """Port one enemy, never raising for a per-enemy failure.

        Outermost safety net only. Every *known* recoverable condition is
        handled by a specific check inside ``_port_enemy``; this catch exists so
        an UNANTICIPATED exception from any porter still costs one enemy's
        appearance instead of a whole world's run. Run-fatal classes
        (``GlobalTexCountError``) are re-raised untouched, as is
        ``KeyboardInterrupt`` / ``SystemExit``.

        With ``MESH_PORT_ENABLED`` False every route is refused HERE, first,
        before a single byte is read out of a source ``.PRS`` and before
        ``self.prs`` / ``self.prt`` / ``self.bef`` can be touched.
        """
        if not MESH_PORT_ENABLED:
            _blob, result = self.refuse(plan, _PARKED_REASON)
            return blob, result
        try:
            returned, result = self._port_enemy(plan, blob)
            # A refusal returns None for the blob; hand the caller back the
            # ORIGINAL bytes so no caller can mistake a refusal for a rewrite.
            return (blob if returned is None else returned), result
        except GlobalTexCountError:
            raise
        except Exception as exc:  # noqa: BLE001 - logged, then refused
            # An unanticipated failure means we do not know how far the port
            # got, so the route is refused rather than downgraded. There is no
            # "port the mesh anyway" path any more: committing an entry whose
            # provenance is uncertain desynchronises the engine's sequential
            # read of the WHOLE target mesh table (observed as a hard freeze on
            # level load), and no enemy is worth that.
            _none, result = self.refuse(
                plan, f"unexpected {type(exc).__name__} during port: {exc}")
            return blob, result

    # -- anim (CATEGORY-2) policy ------------------------------------------
    def _check_local_anim_refs(self, plan: PlacementPlan, refs) -> str | None:
        """Return a refusal reason for this enemy's LOCAL CATEGORY-2 refs, or None.

        POLICY: REFUSE out-of-range, LEAVE in-range.

        A CATEGORY-2 reference indexes the world's ``.PRS`` ANIM table, and the
        anim entry format is UNPARSED -- anim entries therefore CANNOT be
        ported. That leaves exactly two options for a local cat-2 ref, and both
        halves of the choice are forced:

        * OUT OF RANGE -> REFUSE. CASTLE local cat-2 indices run up to 108 while
          GRAVE's anim table has only 53 entries, so leaving such a ref makes
          the engine read past the end of the table. That is an out-of-bounds
          read on level load and a very likely hard freeze. Clamping to a
          "known-valid" index was the alternative; it is rejected because it
          silently fabricates an animation binding for an enemy whose real
          animation set is absent, and a fabricated binding is exactly the class
          of guess that produced the wrong-model regression. Refusing costs one
          enemy and cannot corrupt anything.
        * IN RANGE -> LEAVE IT ALONE. It points at a different animation than
          the source world's, so the enemy may move oddly, but it CANNOT dangle.
          Per the user's earlier decision, wrong animations are tolerable;
          out-of-range reads are not.
        """
        from .bef import MATERIAL_CATEGORY_ANIM

        local = [r for r in refs
                 if not r.is_global and r.category == MATERIAL_CATEGORY_ANIM]
        if not local:
            return None
        anim_count = self.target_anim_count()
        if anim_count is None:
            return (f"the blob carries {len(local)} LOCAL CATEGORY-2 (anim) "
                    f"reference(s) but {self._prs_name}'s anim-table header "
                    f"could not be read, so they cannot be bounds-checked; a "
                    f"dangling anim index is an out-of-range read on level "
                    f"load")
        bad = sorted({r.index for r in local if r.index >= anim_count})
        if bad:
            return (f"LOCAL CATEGORY-2 (anim) reference(s) {bad} are >= "
                    f"{self.target_world.upper()}'s anim count {anim_count}, "
                    f"and anim entries cannot be ported (the anim entry format "
                    f"is unparsed). Leaving them would be an out-of-range read "
                    f"and a likely hard freeze, so this route is REFUSED")
        self.log(f"{plan.name} 0x{plan.enemy_id:02X}: "
                 f"{len(local)} local CATEGORY-2 (anim) ref(s) "
                 f"{sorted({r.index for r in local})} all < "
                 f"{self.target_world.upper()}'s anim count {anim_count}; left "
                 f"as-is (may animate differently, cannot dangle)")
        return None

    # -- anim donor (HEURISTIC SUBSTITUTION, NOT A PORT) -------------------
    def anim_donor_for(self, bone_count: int, needed_slots: int | None = None):
        """Cached ``select_anim_donor`` against this session's PRISTINE buffers.

        Pristine on purpose: the donor must be one of the target world's OWN
        natives, and appended entries are by definition not native.
        """
        key = (bone_count, needed_slots)
        if key in self._anim_donors:
            return self._anim_donors[key]
        donor = select_anim_donor(self.pristine_bef, self.pristine_prs,
                                  self.target_world, bone_count, log=self.log,
                                  needed_slots=needed_slots)
        self._anim_donors[key] = donor
        return donor

    def _cached_anim_donor(self, bone_count: int):
        """Most recently cached donor for ``bone_count``, any slot demand."""
        for (bones, _slots), donor in reversed(list(self._anim_donors.items())):
            if bones == bone_count and donor is not None:
                return donor
        return None

    def _substitute_anim_refs(self, plan, blob, refs, bone_count):
        """Repoint every LOCAL CATEGORY-2 ref at a bone-matched native's anims.

        Returns ``(new_blob, {idx_offset: (flag, new_index)})``; an empty dict
        means nothing was substituted and the blob comes back unchanged (the
        switch is off, there is no local cat-2 ref, no bone count was measured,
        or no donor qualified).

        MAPPING: the enemy's DISTINCT source anim indices, sorted, are laid
        positionally onto the donor's distinct anim indices, sorted. When the
        enemy names more anims than the donor owns (Dark_Knight names 11, Ghost
        owns 3) the surplus all land on the donor's LAST index -- every
        reference then resolves to a real, skeleton-driving entry, which is the
        whole point; picking a wrong-but-valid clip is acceptable, leaving a
        source-world index that binds nothing is not.

        Raises ``_PortRefusal`` if a substituted index is not < the target's
        anim count, so the caller refuses the enemy and mutates nothing.
        """
        from .bef import MATERIAL_CATEGORY_ANIM, set_material_ref_index

        local = [r for r in refs
                 if not r.is_global and r.category == MATERIAL_CATEGORY_ANIM]
        if not ANIM_SUBSTITUTION_ENABLED or not local:
            return blob, {}
        if not bone_count:
            self.log(f"{plan.name} 0x{plan.enemy_id:02X}: no bone count could "
                     f"be measured for its meshes, so no anim donor can be "
                     f"chosen; {len(local)} local CATEGORY-2 ref(s) left as-is")
            return blob, {}

        source_indices = sorted({r.index for r in local})
        donor = self.anim_donor_for(bone_count,
                                    needed_slots=len(source_indices))
        if donor is None:
            self.log(f"{plan.name} 0x{plan.enemy_id:02X}: no anim donor "
                     f"available in {self.target_world.upper()}; "
                     f"{len(local)} local CATEGORY-2 ref(s) left as-is")
            return blob, {}

        anim_count = self.target_anim_count()
        mapping = {}
        for position, src in enumerate(source_indices):
            slot = min(position, len(donor.anim_indices) - 1)
            mapping[src] = donor.anim_indices[slot]

        for src, dst in mapping.items():
            if anim_count is None or not dst < anim_count:
                raise _PortRefusal(
                    f"HEURISTIC anim substitution would point source anim "
                    f"{src} at {self.target_world.upper()} anim {dst}, which "
                    f"is not < the target anim count {anim_count}")

        rewrites: dict[int, tuple[int, int]] = {}
        for ref in local:
            blob = set_material_ref_index(blob, ref, mapping[ref.index])
            rewrites[ref.idx_offset] = (ref.flag, mapping[ref.index])

        delta = abs(donor.bone_count - bone_count)
        self.log(f"ANIM SUBSTITUTION {plan.name} 0x{plan.enemy_id:02X} "
                 f"({bone_count} bones) -> donor native {donor.name} "
                 f"0x{donor.enemy_id:02X} ({donor.bone_count} bones, "
                 f"|delta| = {delta}) in {self.target_world.upper()}: "
                 f"{len(local)} local CATEGORY-2 ref(s), "
                 f"{', '.join(f'{s}->{d}' for s, d in sorted(mapping.items()))}"
                 f"; all substituted indices < anim count {anim_count}")
        if delta > 6:
            self.log(f"  LOW CONFIDENCE: donor bone count differs by {delta}")
        self.log("  HEURISTIC substitution, not a port -- animation may still "
                 "be wrong")
        return blob, rewrites

    # -- anim PORT (the enemy's OWN records) -------------------------------
    def _target_rig_bone_names(self, plan, staged_prs, target_mesh_indices):
        """Bone names of THE RIG THE PORTED RECORDS WILL DRIVE.

        Read from the STAGED ``.PRS`` at the indices this enemy's meshes were
        just staged at -- i.e. from the ported enemy's OWN assets, as they now
        sit in the target archive -- via
        ``mesh_port.mesh_entry_anim_target_names``.

        BOTH NAMESPACES, because an anim record may address either: an SKA
        record names BONES, a MEA record names MESH NODES (measured: CASTLE
        anim[46], Dark_Knight's head/effect clip, addresses ``helmet``, ``nail``,
        ``horn_l`` -- every one of them a mesh-node name of his tag-1 mesh[26],
        none of them a bone of his skeleton mesh[25]). Checking a MEA record
        against bone names alone would refuse a record that binds perfectly.

        THIS IS THE NON-VACUOUS FORM OF THE CHECK. Task 9.3's end-to-end test
        derived the target names from the anim bundle itself, which makes
        ``can_bind_to_rig`` true by construction and therefore proves nothing.
        The names here come from the MESH, an independent artefact parsed by an
        independent parser (``prs_mesh_parser.load_bone``), so a record whose
        channels name bones the rig does not carry is genuinely caught.

        Union across every entry the enemy ported: an enemy names one skinned
        skeleton plus 0-bone props, and only the skeleton contributes names.
        An entry whose bone names cannot be read contributes nothing and is
        logged; if NOTHING can be read the caller gets an empty tuple and the
        porter refuses (an empty target rig cannot carry any name).
        """
        from .mesh_port import mesh_entry_anim_target_names

        names: list[str] = []
        for index in sorted(set(target_mesh_indices)):
            try:
                bones, nodes = mesh_entry_anim_target_names(staged_prs, index)
                entry_names = bones + nodes
                self.log(f"{plan.name} 0x{plan.enemy_id:02X}: staged target "
                         f"mesh[{index}] carries {len(bones)} bone name(s) and "
                         f"{len(nodes)} mesh-node name(s)")
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                self.log(f"{plan.name} 0x{plan.enemy_id:02X}: bone names of "
                         f"staged target mesh[{index}] could not be read "
                         f"({type(exc).__name__}: {exc}); it contributes no "
                         f"name to the target rig")
                continue
            for name in entry_names:
                if name and name not in names:
                    names.append(name)
        return tuple(names)

    def _port_own_anim_records(self, plan, staged_prs, blob, refs,
                               source_world, target_mesh_indices):
        """Append this enemy's OWN anim records into ``staged_prs``.

        Returns an ``anim_port.AnimPortOutcome``. A non-ok outcome is a REFUSAL
        of the enemy (Requirement 9.5) -- the caller must not fall back to the
        substitution heuristic, and nothing here has touched ``self``.

        ORDERING (task 9.5): this runs AFTER every mesh entry is staged, and the
        anim region is re-located from ``staged_prs`` inside
        ``port_anim_records`` itself, so no anchor is ever held across a mesh
        insert.

        PROVENANCE, asserted rather than assumed: the anim records and the mesh
        come from the SAME source enemy in the SAME source world, which is what
        makes the bone names comparable at all. Both facts are checked here.
        """
        from .anim_port import collect_source_records, port_anim_records
        from .bef import MATERIAL_CATEGORY_ANIM

        from .bef import parse_material_refs

        sw = str(source_world).upper()
        # RE-PARSE the refs from the blob AS IT IS NOW. The refs the caller
        # parsed describe the blob BEFORE the CATEGORY-1 mesh rewrites, so their
        # cat-1 indices are stale, and ``port_anim_records`` proves its own
        # rewrite by re-parsing and comparing ref for ref -- it must be handed
        # the refs OF THIS BLOB or every already-rewritten cat-1 ref reads as an
        # unexplained change. CATEGORY-2 refs are untouched by the mesh step, so
        # their source anim indices survive the re-parse unchanged.
        refs = parse_material_refs(blob)

        # REFS THE PARSER MISSES. `parse_material_refs` reads resource refs only
        # from the START of each record body -- it walks
        # `while blob[p] in MATERIAL_REC_TYPES` from `body` and stops at the
        # first byte that is not 0x04/0x05. A record whose body does NOT begin
        # with a ref contributes NOTHING, even though a ref sits deeper inside it
        # encoded identically.
        #
        # MEASURED on Goat_Devil 0x13 (UNDER.BEF): three cat-2 refs at 0x12E
        # (anim 78), 0x162 (anim 78) and 0x1F8 (anim 89) are invisible to the
        # parser. Their indices were therefore never remapped, so in the target
        # world they addressed whatever NATIVE animation happened to occupy 78
        # and 89 -- the goat played wrong clips, which is why he animated but
        # never closed on the player.
        #
        # This is the ROOT-enemy path. `companion_port.port_companion` carries
        # the same recovery for COMPANIONS; an enemy with no companions (the
        # goat has none) reaches only this one.
        from .companion_port import missed_local_refs
        missed = missed_local_refs(blob)
        if missed:
            self.log(
                f"{plan.name} 0x{plan.enemy_id:02X}: {len(missed)} resource "
                f"ref(s) are INVISIBLE to parse_material_refs, at "
                + ", ".join(f"0x{r.idx_offset:X}(cat{r.category} idx{r.index})"
                            for r in missed)
                + " -- previously left pointing at SOURCE-world resources")

        local = [r for r in refs
                 if not r.is_global and r.category == MATERIAL_CATEGORY_ANIM]
        missed_local = [r for r in missed
                        if r.category == MATERIAL_CATEGORY_ANIM]
        wanted = sorted({r.index for r in local}
                        | {r.index for r in missed_local})
        if missed:
            self.log(f"{plan.name} 0x{plan.enemy_id:02X}: anim set is now "
                     f"{wanted} ({len(wanted)} record(s))")

        target_names = self._target_rig_bone_names(plan, staged_prs,
                                                   target_mesh_indices)
        self.log(f"{plan.name} 0x{plan.enemy_id:02X}: target rig is its OWN "
                 f"ported mesh(es) {sorted(set(target_mesh_indices))} in "
                 f"{self.target_world.upper()}, {len(target_names)} bone "
                 f"name(s) read from the staged .PRS "
                 f"(same-source provenance: mesh and anim records both come "
                 f"from {sw}); every source record's channel names are checked "
                 f"against them by name")

        # The source .PRS is read ONCE, gated, and dropped before the target
        # buffer is grown -- one .PRS resident at a time (Requirement 10.3).
        source_prs = self._source_path(sw, "PRS")
        try:
            source_bytes = source_prs.read_bytes()
        except (FileNotFoundError, OSError) as exc:
            from .anim_port import AnimPortOutcome
            return AnimPortOutcome(
                ok=False,
                reasons=[f"source {source_prs.name} could not be re-read for "
                         f"its anim records: {type(exc).__name__}: {exc}"])
        try:
            bundle = collect_source_records(source_bytes, wanted,
                                            source_world=sw,
                                            label=source_prs.name)
        finally:
            del source_bytes

        outcome = port_anim_records(
            staged_prs, blob, refs, bundle, target_names,
            memo=self.anim_memo, source_world=sw,
            label=f"{self.target_world.upper()}.PRS",
            extra_refs=missed)
        for line in outcome.details:
            self.log(f"anim port {line}")
        return outcome

    # -- one mesh entry ----------------------------------------------------
    def _port_one_mesh(self, plan, staged_prs, staged_prt, source_world,
                       source_index, entry_bytes, reserved):
        """Port ONE mesh entry into ``staged_prs`` / ``staged_prt``.

        Operates purely on the staged buffers handed in and returns new ones;
        ``self.prs`` / ``self.prt`` are not touched, so an exception anywhere
        leaves the session exactly as it was. Raises ``_PortRefusal`` for every
        recoverable problem so the caller can refuse the WHOLE enemy.

        Steps 2-7 of the design, in order: trial-insert into a throwaway buffer
        so the parser has a real container to walk, enumerate the entry's render
        passes, port EVERY id it binds with ``texture_port`` (they are all
        source-world-local ``.PRT`` indices), patch the ids IN PLACE inside
        the real container, extract the patched entry back out, and only then
        insert it into the staged buffer. No ``.PRS`` is ever synthesised.
        """
        from . import prs_mesh_parser as pmp
        from .mesh_port import extract_mesh_entry_from_buffer, insert_mesh_entry
        from .texture_port import PRTPortError, extract_texture, inject_texture

        label = f"{plan.name} 0x{plan.enemy_id:02X} src mesh[{source_index}]"

        try:
            temp_prs, new_index = insert_mesh_entry(staged_prs, entry_bytes)
        except (ValueError, IndexError, pmp.PRSParseError) as exc:
            raise _PortRefusal(f"trial insert of {label} failed: "
                               f"{type(exc).__name__}: {exc}") from exc

        # Parser paths look like "mesh[39]@0x1E4910...", so the trailing "@"
        # makes prefix matching exact: without it "mesh[4]" would also match
        # "mesh[40]".
        prefix = f"mesh[{new_index}]@"
        try:
            with pmp.parser_scope():
                reader = pmp.Reader(temp_prs, _MESH_COUNT_OFFSET)
                pmp.load_mesh_res_table(reader)
                passes = pmp.find_render_passes_under(prefix)
                tex_ids = sorted({tid for _o, tid, _p in passes if tid >= 0})
        except (pmp.PRSParseError, IndexError, struct.error) as exc:
            raise _PortRefusal(f"parse of the trial container for {label} "
                               f"failed: {type(exc).__name__}: "
                               f"{exc}") from exc

        # An entry that binds NO texture id is legitimate here, unlike in the
        # offset-22 era. Back then "clean parse, all ids -1" was evidence the
        # bytes were not the enemy's model, because the index came from a field
        # that did not name a mesh. Now the index comes from the blob's own
        # CATEGORY-1 material reference, so the entry IS what the enemy asks
        # for -- Bomb_Skeleton's third ref (CASTLE mesh[12]) is exactly such a
        # prop. Port it with an empty remap.
        # EVERY id here is SOURCE-WORLD-LOCAL: a render-pass texture id is a
        # direct index into the SOURCE world's own ``.PRT``, so all of them
        # must be ported and remapped. There is deliberately no
        # ``id < global_tex_count -> leave it alone`` branch: that partition
        # rested on the disproven absolute-shared-cache model. Evidence:
        # CASTLE.PRS mesh[10] binds {9,10,11,12} and ICE.PRS mesh[18] -- the
        # same 157,356-byte asset -- binds {21,22,23,24}, and CASTLE.PRT 9-12
        # are BYTE-IDENTICAL to ICE.PRT 21-24 (headers, pixels and CLUTs).
        # Under the old rule all of those sub-164 ids were left untouched, so a
        # ported entry kept pointing at whatever unrelated texture occupies
        # that slot in the target world -- the wrong-skin bug.
        local_ids = list(tex_ids)
        self.log(f"{label}: {len(passes)} render pass(es), texture ids "
                 f"{tex_ids or '(none bound)'} -> {len(local_ids)} "
                 f"source-world-local .PRT index(es) to port")

        remap: dict[int, int] = {}
        ported = 0
        if local_ids:
            source_prt = self._source_path(source_world, "PRT")
            for tid in local_ids:
                src_local = tid
                memo_key = (source_world.upper(), src_local)
                if memo_key in self.tex_memo:
                    remap[tid] = self.tex_memo[memo_key]
                    self.log(f"{label}: tex {tid} already ported this run -> "
                             f"{remap[tid]}")
                    continue
                try:
                    tex = extract_texture(str(source_prt), src_local,
                                          source_world.upper())
                    staged_prt = inject_texture(staged_prt, tex)
                except (PRTPortError, FileNotFoundError, OSError, IndexError,
                        struct.error) as exc:
                    raise _PortRefusal(
                        f"texture port of source local index {src_local} from "
                        f"{source_prt.name} for {label} failed: "
                        f"{type(exc).__name__}: {exc}") from exc
                new_id = self.assign_tex_index()
                reserved.append(new_id)
                remap[tid] = new_id
                self.tex_memo[memo_key] = new_id
                ported += 1

        patched_prs = temp_prs
        if remap:
            try:
                with pmp.parser_scope():
                    reader = pmp.Reader(temp_prs, _MESH_COUNT_OFFSET)
                    pmp.load_mesh_res_table(reader)
                    patched_prs, patch_log = pmp.patch_texture_ids(
                        temp_prs, prefix, remap)
            except (pmp.PRSParseError, IndexError, struct.error) as exc:
                raise _PortRefusal(f"texture-id patch for {label} failed: "
                                   f"{type(exc).__name__}: {exc}") from exc
            self.log(f"{label}: patched {len(patch_log)} render-pass texture "
                     f"id field(s), remap "
                     f"{', '.join(f'{o}->{n}' for o, n in sorted(remap.items()))}")

        try:
            patched_entry, _tag = extract_mesh_entry_from_buffer(patched_prs,
                                                                new_index)
        except (ValueError, IndexError, pmp.PRSParseError) as exc:
            raise _PortRefusal(f"re-extraction of the patched entry for "
                               f"{label} failed: {type(exc).__name__}: "
                               f"{exc}") from exc

        staged_prs, final_index = insert_mesh_entry(staged_prs, patched_entry)
        del temp_prs, patched_prs
        return staged_prs, staged_prt, final_index, remap, ported

    # -- the whole enemy ---------------------------------------------------
    def _port_enemy(self, plan: PlacementPlan, blob: bytes) -> tuple[bytes, PortResult]:
        """Port one enemy's referenced meshes (and their world-local textures)
        into this session's buffers, driven by the blob's MATERIAL REFERENCES.

        Algorithm:
          a. parse the source blob's material refs;
          b. gate the TARGET ``.PRS`` (clean walk + anim-anchor agreement);
          c. leave every GLOBAL ref strictly alone -- that asset exists in
             every world;
          d. for every LOCAL CATEGORY-1 ref, gate the source entry, port each
             DISTINCT source index ONCE, and rewrite every ref that names it to
             the one new index, preserving the flag byte;
          e. port and remap any source-world-local textures the ported entries
             reference;
          f. bounds-check LOCAL CATEGORY-2 (anim) refs -- see
             ``_check_local_anim_refs``;
          g. commit only if every step succeeded.

        ALL-OR-NOTHING: every mutation is staged on locals and assigned to
        ``self.prs`` / ``self.prt`` in one final commit, so a refusal anywhere
        leaves the session byte-identical to its pre-port state.
        """
        from .bef import (
            MATERIAL_CATEGORY_MESH,
            MaterialSectionError,
            parse_material_refs,
            set_material_ref_index,
        )
        from . import prs_mesh_parser as pmp
        from .mesh_port import assess_source_entry, mesh_entry_signature

        source_world = plan.source_world
        if not source_world:
            return blob, self._fail(plan, "plan carries no source world")
        sw = source_world.upper()

        # --- a. the blob's own resource references ------------------------
        try:
            refs = parse_material_refs(blob)
        except MaterialSectionError as exc:
            return self.refuse(
                plan, f"the source blob's material section does not walk, so "
                      f"its resource references cannot be trusted: {exc}")
        self.log(f"{plan.name} 0x{plan.enemy_id:02X} {sw} blob: "
                 f"{len(refs)} material reference(s)")
        for ref in refs:
            self.log(f"  ref {ref.describe()}")

        # --- b. TARGET TABLE GATE ----------------------------------------
        # The insertion point IS this walk's end, so a walk that needed a
        # resync (CASTLE, SWAMP and UNDER all land 166-720 KB short of their
        # anim anchors) makes the target unusable and the world is refused.
        target_trust = self.target_trust()
        if not target_trust.ok:
            return self.refuse(plan, target_trust.reason, target_trust.details)

        # --- f. anim refs, before anything is read or staged -------------
        # SAFE HERE only because this is a BOUNDS CHECK against the table's
        # count WORD, and a mesh insert shifts the anim table without changing
        # that word. Anything that needs an anim OFFSET -- an anchor, a region,
        # an append (the task 10.1 route) -- must NOT run here: it has to run
        # AFTER step e's mesh inserts, re-locating from the staged buffer via
        # ``locate_anim_region(staged_prs)``, because ``insert_mesh_entry``
        # moves every anim byte later in the file.
        # --- THE ROUTING DECISION (task 10.1, Requirements 9.1-9.4) -------
        # Short-circuit order matters: with the switch off ``anim_trust()`` is
        # never called, so a run with ANIM_PORT_ENABLED False assesses nothing
        # extra and logs nothing extra -- it behaves exactly as it did before
        # this branch existed.
        route_anim_port = ANIM_PORT_ENABLED and self.anim_trust().ok
        if route_anim_port:
            # Logged ONCE per enemy, through self.log, which carries the
            # [cross-world] prefix (Requirements 9.4, 10.6).
            self.log(f"ANIM PATH {plan.name} 0x{plan.enemy_id:02X}: PORT -- "
                     f"ANIM_PORT_ENABLED and {self.target_world.upper()}'s anim "
                     f"trust gate passes, so this enemy's OWN anim records are "
                     f"appended and its refs repointed; a refusal from the "
                     f"porter refuses this ENEMY and does NOT fall back to the "
                     f"substitution heuristic")
        else:
            self.log(f"ANIM PATH {plan.name} 0x{plan.enemy_id:02X}: "
                     f"SUBSTITUTION HEURISTIC -- "
                     + ("ANIM_PORT_ENABLED is False"
                        if not ANIM_PORT_ENABLED
                        else f"{self.target_world.upper()}'s anim trust gate "
                             f"fails")
                     + "; LOCAL CATEGORY-2 refs are handled exactly as before")
            anim_problem = self._check_local_anim_refs(plan, refs)
            if anim_problem:
                return self.refuse(plan, anim_problem)

        # --- c. GLOBAL refs are never touched ----------------------------
        global_refs = [r for r in refs if r.is_global]
        if global_refs:
            self.log(f"{plan.name} 0x{plan.enemy_id:02X}: "
                     f"{len(global_refs)} GLOBAL ref(s) left untouched "
                     f"(cat/index "
                     f"{', '.join(f'{r.category}/{r.index}' for r in global_refs)})")

        local_cat1 = [r for r in refs
                      if not r.is_global and r.category == MATERIAL_CATEGORY_MESH]
        if not local_cat1:
            # No local mesh reference at all: every mesh this enemy asks for is
            # GLOBAL, so the blob is already correct for any world. Queue it
            # verbatim; no .PRS or .PRT mutation is needed or performed.
            self.pending_blobs.append((plan.enemy_id, blob))
            self.log(f"{plan.name} 0x{plan.enemy_id:02X}: no LOCAL CATEGORY-1 "
                     f"reference; every mesh is GLOBAL, blob queued verbatim")
            result = PortResult(plan=plan, ok=True, mesh_index=None,
                                original_blob=blob)
            self.results.append(result)
            self._note_no_anim(plan)
            return blob, result

        # DEDUPE: distinct source indices only. Dark_Knight 0x53 names 25
        # twice and Bomb_Skeleton 0x49 names 10 eight times; each is ported
        # ONCE and every naming ref is repointed at the same new index.
        distinct = sorted({r.index for r in local_cat1})
        self.log(f"{plan.name} 0x{plan.enemy_id:02X}: "
                 f"{len(local_cat1)} local CATEGORY-1 ref(s) naming "
                 f"{len(distinct)} distinct source mesh index(es) {distinct}")

        # --- d. gate every distinct source entry, reading the source .PRS
        #        exactly once and dropping it immediately -----------------
        source_prs = self._source_path(source_world, "PRS")
        entries: dict[int, bytes] = {}
        signatures: dict[int, tuple] = {}
        try:
            source_bytes = source_prs.read_bytes()
        except (FileNotFoundError, OSError) as exc:
            return self.refuse(
                plan, f"source {source_prs.name} could not be read: "
                      f"{type(exc).__name__}: {exc}")
        try:
            for index in distinct:
                if (sw, MATERIAL_CATEGORY_MESH, index) in self.mesh_memo:
                    continue
                try:
                    verdict = assess_source_entry(source_bytes, index,
                                                  label=source_prs.name)
                except (pmp.PRSParseError, ValueError, IndexError,
                        struct.error) as exc:
                    return self.refuse(
                        plan, f"source {source_prs.name} mesh table could not "
                              f"be walked as far as mesh[{index}]: "
                              f"{type(exc).__name__}: {exc}")
                for line in verdict.details:
                    self.log(f"gate {line}")
                if not verdict.ok:
                    return self.refuse(plan, verdict.reason, verdict.details)
                entries[index] = verdict.entry_bytes
                signatures[index] = mesh_entry_signature(source_bytes, index)
                self.sig_memo[(sw, MATERIAL_CATEGORY_MESH, index)] = \
                    signatures[index]
                self.log(f"gate PASS {plan.name} 0x{plan.enemy_id:02X} "
                         f"{source_prs.name} mesh[{index}]: signature "
                         f"(len,tag,bones,passes)={signatures[index]}")
        finally:
            del source_bytes

        # --- e. stage every entry -----------------------------------------
        staged_prs = self.prs
        staged_prt = self.prt
        reserved: list[int] = []
        new_indices: dict[int, int] = {}
        remap_all: dict[int, int] = {}
        ported_meshes = 0
        ported_tex = 0
        for index in distinct:
            memo_key = (sw, MATERIAL_CATEGORY_MESH, index)
            if memo_key in self.mesh_memo:
                new_indices[index] = self.mesh_memo[memo_key]
                self.log(f"{plan.name} 0x{plan.enemy_id:02X}: source mesh"
                         f"[{index}] already ported this session -> target "
                         f"mesh[{new_indices[index]}] (deduped)")
                continue
            try:
                staged_prs, staged_prt, new_index, remap, ported = \
                    self._port_one_mesh(plan, staged_prs, staged_prt,
                                        source_world, index, entries[index],
                                        reserved)
            except _PortRefusal as exc:
                self._rollback_reserved(reserved)
                return self.refuse(plan, str(exc))
            new_indices[index] = new_index
            remap_all.update(remap)
            ported_meshes += 1
            ported_tex += ported
            self.log(f"{plan.name} 0x{plan.enemy_id:02X}: source mesh[{index}] "
                     f"staged at target mesh[{new_index}] "
                     f"({len(entries[index])} B)")

        # --- rewrite the refs, flags preserved ---------------------------
        patched_blob = blob
        rewrites: dict[int, tuple[int, int]] = {}
        for ref in local_cat1:
            new_index = new_indices[ref.index]
            patched_blob = set_material_ref_index(patched_blob, ref, new_index)
            rewrites[ref.idx_offset] = (ref.flag, new_index)

        # --- the anim branch: PORT, or HEURISTIC SUBSTITUTION -------------
        # Routed by ``route_anim_port``, decided and logged once per enemy at
        # step f. Both arms run HERE, AFTER every mesh entry is staged, which is
        # the ordering rule the port arm must obey: mesh first, then the anim
        # region re-located from ``staged_prs`` (``port_anim_records`` does that
        # itself, so no anchor is held across a mesh insert). Substitution
        # appends nothing and needs no count refresh; the port arm refreshes the
        # cached count at the commit below, against the count it reported.
        # Bone count of this enemy's skeletal mesh: the max across its local
        # cat-1 entries (an enemy names one skinned skeleton plus 0-bone props).
        # Signatures come from sig_memo so a deduped entry still contributes.
        bones = [self.sig_memo[(sw, MATERIAL_CATEGORY_MESH, index)][2]
                 for index in distinct
                 if (sw, MATERIAL_CATEGORY_MESH, index) in self.sig_memo]
        bone_count = max(bones) if bones else None
        anim_rewrites: dict[int, tuple[int, int]] = {}
        anim_port_rewrites: dict[int, tuple[int, int]] = {}
        ported_anim_indices: dict[int, int] = {}
        anim_outcome = None
        if route_anim_port:
            # THE PORT BRANCH. A refusal here refuses the ENEMY -- there is no
            # fallback to the heuristic (Requirement 9.5).
            anim_outcome = self._port_own_anim_records(
                plan, staged_prs, patched_blob, refs, sw,
                list(new_indices.values()))
            if not anim_outcome.ok:
                self._rollback_reserved(reserved)
                return self.refuse(
                    plan,
                    f"the anim PORT refused this enemy, and a refused port is "
                    f"NEVER downgraded to the substitution heuristic "
                    f"(Requirement 9.5): {anim_outcome.reason}",
                    anim_outcome.details)
            # Adopt the staged .PRS and blob ONLY TOGETHER, and only here where
            # the port succeeded in full. The memo and the anim-count refresh
            # follow at the commit below, alongside ``self.prs``.
            staged_prs = anim_outcome.prs
            patched_blob = anim_outcome.blob
            anim_port_rewrites = dict(anim_outcome.rewrites)
            ported_anim_indices = {src: dst for (_w, src), dst
                                   in anim_outcome.new_indices.items()}
        else:
            try:
                patched_blob, anim_rewrites = self._substitute_anim_refs(
                    plan, patched_blob, refs, bone_count)
            except _PortRefusal as exc:
                self._rollback_reserved(reserved)
                return self.refuse(plan, str(exc))
        anim_expected = {off: idx for off, (_f, idx)
                         in {**anim_rewrites, **anim_port_rewrites}.items()}

        # Prove the rewrite: re-parse and compare ref for ref. A flag byte that
        # moved would have changed the category or the global bit, which
        # repoints the reference at a different resource table entirely.
        try:
            after = parse_material_refs(patched_blob)
        except MaterialSectionError as exc:
            self._rollback_reserved(reserved)
            return self.refuse(
                plan, f"the rewritten blob's material section no longer walks: "
                      f"{exc}")
        if len(after) != len(refs):
            self._rollback_reserved(reserved)
            return self.refuse(
                plan, f"the rewritten blob parses {len(after)} material "
                      f"reference(s), the source had {len(refs)}")
        for before, now in zip(refs, after):
            if (now.flag, now.idx_offset, now.rec_type) != (
                    before.flag, before.idx_offset, before.rec_type):
                self._rollback_reserved(reserved)
                return self.refuse(
                    plan, f"rewrite changed more than an index: "
                          f"{before.describe()} became {now.describe()}")
            if (not before.is_global
                    and before.category == MATERIAL_CATEGORY_MESH):
                expected = new_indices[before.index]
            elif before.idx_offset in anim_expected:
                expected = anim_expected[before.idx_offset]
            else:
                expected = before.index
            if now.index != expected:
                self._rollback_reserved(reserved)
                return self.refuse(
                    plan, f"ref at 0x{before.idx_offset:X} should now read "
                          f"index {expected} but reads {now.index}")

        # --- g. COMMIT ----------------------------------------------------
        prs_growth = len(staged_prs) - len(self.prs)
        prt_growth = len(staged_prt) - len(self.prt)
        self.prs = staged_prs
        self.prt = staged_prt
        for index, new_index in new_indices.items():
            self.mesh_memo[(sw, MATERIAL_CATEGORY_MESH, index)] = new_index
        appended_anims = 0
        if anim_outcome is not None:
            # The staged buffer, the staged blob and the staged anim memo are
            # adopted as ONE unit; and the cached anim count is invalidated
            # against the count the append reported, so the next enemy of this
            # session bounds-checks against the table that now exists (the
            # stale-bound hazard of task 9.5).
            self.anim_memo.update(anim_outcome.memo)
            appended_anims = anim_outcome.appended_count
            if appended_anims:
                self.refresh_anim_count(expected=anim_outcome.new_count)
        self.pending_blobs.append((plan.enemy_id, patched_blob))

        self.log(f"{plan.name} 0x{plan.enemy_id:02X} committed: "
                 f"{ported_meshes} mesh entr"
                 f"{'y' if ported_meshes == 1 else 'ies'} appended "
                 f"(+{prs_growth} B PRS), {ported_tex} texture(s) ported "
                 f"(+{prt_growth} B PRT), mesh count now {self.mesh_count()}, "
                 f"{len(rewrites)} ref(s) rewritten "
                 f"{sorted((src, dst) for src, dst in new_indices.items())}")

        result = PortResult(plan=plan, ok=True,
                            mesh_index=min(new_indices.values()),
                            tex_remap=remap_all, ported_textures=ported_tex,
                            mesh_committed=ported_meshes > 0,
                            mesh_indices=dict(new_indices),
                            ported_meshes=ported_meshes,
                            ref_rewrites=rewrites, original_blob=blob,
                            signatures=signatures,
                            anim_rewrites=anim_rewrites,
                            anim_donor=(self._cached_anim_donor(bone_count)
                                        if anim_rewrites else None),
                            bone_count=bone_count,
                            anim_port_rewrites=anim_port_rewrites,
                            ported_anim_indices=ported_anim_indices,
                            appended_anims=appended_anims)
        self.results.append(result)
        if anim_outcome is not None:
            self.log(f"ANIM PORT {plan.name} 0x{plan.enemy_id:02X}: "
                     f"{appended_anims} record(s) appended, anim count "
                     f"{anim_outcome.old_count} -> {anim_outcome.new_count}, "
                     f"{len(anim_port_rewrites)} LOCAL CATEGORY-2 ref(s) "
                     f"repointed "
                     f"{sorted(ported_anim_indices.items())}")
            # NO _note_no_anim on this branch: animation data WAS ported, so the
            # note would be false (task 10.1).
        else:
            self._note_no_anim(plan)
        return patched_blob, result

    # -- BEF rebuild (removed) ---------------------------------------------
    # A local order-preserving rebuild used to live here as a workaround for an
    # off-by-8*N bug in ``bef.rebuild_with_injections(replace=True)``: it
    # started the data section at ``HEADER_SIZE + OLD toc_count*8`` while
    # writing a TOC with the NEW count, so every recorded blob offset was 8
    # bytes per appended entry too small and EVERY entry -- progression anchors
    # included -- was misaligned. That bug is FIXED in bef.py (offsets are now
    # derived from the actual write position), and the fixed output was verified
    # byte-identical to the workaround on the real Dark_Knight +
    # Zombie_Crocodile -> GRAVE case (122,017 B, 0 differing bytes). Do not
    # reintroduce a local rebuild; if offsets ever look wrong again, check that
    # bef.py still derives them from ``data_start + len(data_section)``.
    # -- batched finish ----------------------------------------------------
    def finish(self) -> dict[str, bytes]:
        """Rebuild the target ``.BEF`` ONCE with every queued blob and return
        the changed buffers.

        Return shape: ``{"<WORLD>.PRS": bytes, "<WORLD>.PRT": bytes,
        "<WORLD>.BEF": bytes}`` keyed by uppercase world name plus extension,
        containing ONLY the files that actually changed (a run with no
        successful port returns an empty dict). Keys match the filenames
        ``iso_patcher.write_patched_assets`` expects.
        """
        from .bef import BefEntry, BefFile  # lazy: bef imports would cycle

        # Companion blobs ride along in the SAME single rebuild. They are always
        # APPENDS -- ``companion_port`` only ever allocates a type id absent from
        # the target -- so they never contribute to ``replace``, and no eviction
        # path is reachable from here at all.
        queued = [(tid, blob) for tid, blob in self.pending_blobs]
        queued += [(tid, blob) for tid, blob, *_ in self.companion_blobs]
        if queued:
            target = BefFile.parse(self.bef)
            injected = [BefEntry(type_id=tid, data_offset=0, data=blob)
                        for tid, blob in queued]
            replace = any(target.has_type(tid) for tid, _ in self.pending_blobs)
            self.bef = target.rebuild_with_injections(injected, replace=replace)
            self.log(f"{self.target_world.upper()}: BEF rebuilt once with "
                     f"{len(injected)} injection(s) "
                     f"({len(self.companion_blobs)} companion), "
                     f"{len(self.pristine_bef)} -> {len(self.bef)} B")

        out: dict[str, bytes] = {}
        if self.prs != self.pristine_prs:
            out[self._prs_name] = self.prs
        if self.prt != self.pristine_prt:
            out[self._prt_name] = self.prt
        if self.bef != self.pristine_bef:
            out[self._bef_name] = self.bef
        self.log(f"{self.target_world.upper()}: finish() -> "
                 f"{sorted(out) or 'no changes'}")
        return out


# --- Run orchestration -----------------------------------------------------

def _run_companion_experiment(session, game_files_dir, emit) -> None:
    """Port ``COMPANION_EXPERIMENT_TYPE_IDS`` into ``session`` as STANDALONE
    entities keeping their ORIGINAL type ids. Default-off; see the switch.

    Uses ``session.port_enemy`` unchanged, so every trust gate, the mesh /
    texture / anim porters and the append-only BEF injection all behave exactly
    as they do for an allowlisted enemy. The only thing bypassed is
    ``classify_placement``, which tests enemy-placement pools that do not apply
    to an entity no level places.
    """
    from .bef import BefFile

    source_world = COMPANION_EXPERIMENT_SOURCE_WORLD
    source_bef = Path(game_files_dir) / f"{source_world.upper()}.BEF"
    emit(f"EXPERIMENT companion-edge probe: injecting "
         f"{', '.join(f'0x{t:02X}' for t in COMPANION_EXPERIMENT_TYPE_IDS)} "
         f"from {source_world.upper()} into "
         f"{session.target_world.upper()} as standalone entities under their "
         f"ORIGINAL type ids (independent of COMPANION_PORT_ENABLED)")
    if not source_bef.exists():
        emit(f"EXPERIMENT refused: {source_bef.name} is missing")
        return
    try:
        parsed = BefFile.parse(source_bef)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        emit(f"EXPERIMENT refused: {source_bef.name} does not parse "
             f"({type(exc).__name__}: {exc})")
        return
    for type_id in COMPANION_EXPERIMENT_TYPE_IDS:
        if not parsed.has_type(type_id):
            emit(f"EXPERIMENT refused: {source_bef.name} declares no type "
                 f"0x{type_id:02X}")
            continue
        blob = parsed.get_entry(type_id).data
        plan = PlacementPlan(
            enemy_id=type_id, name=f"EXPERIMENT_0x{type_id:02X}",
            target_world=session.target_world, path=PORT,
            source_world=source_world, status="EXPERIMENT",
        )
        session.port_enemy(plan, blob)


def port_structure_route(session, *, type_id: int, name: str,
                         source_world: str, companions, game_files_dir,
                         emit=None):
    """Port ONE structure route into ``session``. Returns ``(ok, reason)``.

    THE EXISTING PATH, NOT A NEW ONE (`cross-world-structures` Req 1.1, 1.4).
    This function contains no porting logic at all: it hand-builds a
    ``PlacementPlan(path=PORT)`` and hands it to ``companion_port.port_group``,
    which calls ``session.port_enemy`` unchanged -- exactly what
    ``_run_companion_experiment`` above and
    ``research/tools/measure_structure_route_costs.py`` already do, and exactly
    what Task 1 measured to be entity-generic: nothing on the ``port_enemy`` /
    ``_port_enemy`` path inspects what KIND of entity it is handling, and
    ``enemy_id`` is a name plus the BEF type id to inject under, never a
    discriminator. The source mesh-table walk, the target mesh trust gate, the
    anim trust gate and rig bind check, the texture remap, the
    ``missed_local_refs`` recovery and the append-only BEF injection all run
    UNMODIFIED. No gate is relaxed and no gate is duplicated.

    ``classify_placement`` is deliberately NOT called: its step 1 refuses
    anything outside ``WALKING_MELEE_POOL``, so every structure classifies
    ``REFUSE_NOT_MELEE`` there. That function is a separate static classifier the
    port path neither calls nor needs (Task 1's finding), and bypassing it is not
    bypassing a trust gate -- the enemy-pool test simply does not describe an
    entity the enemy placer never places.

    ``companions`` is the MEASURED closure from the route's cost row, passed
    straight through. ``port_group`` re-resolves the closure from the source
    bytecode and REFUSES if the two disagree, so a stale row cannot port.

    A refusal REFUSES THIS ROUTE ONLY and mutates nothing (Req 1.5):
    ``port_group`` snapshots the session and restores it before recording exactly
    one refusal, so the target world's buffers equal what they would have been
    had the route not been attempted.
    """
    from . import befscript
    from . import companion_port
    from .bef import BefFile

    emit = emit if emit is not None else _default_log
    target = session.target_world
    src = str(source_world)
    tag = (f"0x{int(type_id):02X} {name} {src.upper()}->{target.upper()}")

    source_path = Path(game_files_dir) / f"{src.upper()}.BEF"
    if not source_path.is_file():
        return False, (f"S1 {source_path.name} is missing, so {tag} has no "
                       f"source blob to port")
    try:
        source_bef = BefFile.parse(source_path)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return False, (f"S1 {source_path.name} does not parse "
                       f"({type(exc).__name__}: {exc}), so {tag} is refused")
    if not source_bef.has_type(int(type_id)):
        return False, (f"S1 {source_path.name} declares no type "
                       f"0x{int(type_id):02X}, so {tag} is refused")

    blob = source_bef.get_entry(int(type_id)).data
    plan = PlacementPlan(
        enemy_id=int(type_id), name=str(name), target_world=target,
        path=PORT, source_world=src, status="CANDIDATE")
    oracle = companion_port.make_spawn_edge_oracle(source_bef,
                                                   befscript.REVISION_RETAIL)
    outcome = companion_port.port_group(session, plan, blob,
                                        source_bef=source_bef, edges=oracle,
                                        companions=tuple(companions))
    del source_bef      # one source .BEF at a time, per route
    if not outcome.ok:
        return False, (outcome.reason or f"{tag} was refused by the gated port "
                                        f"path with no reason recorded")
    emit(f"STRUCTURE {tag} ported: +{outcome.prs_growth} B .PRS / "
         f"+{outcome.prt_growth} B .PRT / +{outcome.bef_growth} B .BEF, "
         f"{outcome.companion_count} companion(s), "
         f"{outcome.injected_count} injected")
    return True, None


def port_enemy_route(session, *, type_id: int, source_world=None,
                     game_files_dir, allowlist=None, emit=None):
    """Port ONE ENEMY route into ``session``. Returns ``(ok, reason)``.

    THE EXISTING PATH, LIFTED OUT OF ``run_unified_cross_world``'s ``port_fn``
    SO THERE IS EXACTLY ONE OF IT. The body is that arm verbatim: the
    permission oracle (``classify_placement`` against the allowlist), the
    source ``.BEF`` read and immediate drop, then either
    ``_port_group_with_companions`` or ``session.port_enemy`` depending on
    ``COMPANION_PORT_ENABLED``, and the outcome read off the session's OWN
    fresh results rather than predicted. Every trust gate, the texture remap,
    the anim trust gate, the rig bind check and the append-only BEF injection
    run UNMODIFIED, and a refusal refuses THIS route only.

    ``boss_arena`` calls this with a session whose ``archive_stem`` is an
    arena's, which is the whole of how an arena becomes a porting target: no
    second porter exists.
    """
    from .bef import BefFile

    emit = emit if emit is not None else _default_log
    world = session.target_world
    key = (int(type_id), str(source_world or "?"), world)
    plan = classify_placement(int(type_id), world,
                              cross_world=True, allowlist=allowlist)
    if plan.path != PORT:
        return False, (plan.reason or f"{key} classified {plan.path}, not PORT")
    src = (plan.source_world or source_world or "")
    source_path = Path(game_files_dir) / f"{str(src).upper()}.BEF"
    if not source_path.is_file():
        return False, (f"{source_path.name} is missing, so {key} has no "
                       f"source blob to port")
    try:
        source_bef = BefFile.parse(source_path)
        blob = source_bef.get_entry(int(type_id)).data
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return False, (f"{key} blob unreadable in {source_path.name} "
                       f"({type(exc).__name__}: {exc})")
    del source_bef          # one source .BEF at a time
    before = len(session.results)
    if COMPANION_PORT_ENABLED:
        _port_group_with_companions(session, plan, blob, game_files_dir, emit)
    else:
        session.port_enemy(plan, blob)
    fresh = session.results[before:]
    if any(r.ok for r in fresh):
        return True, None
    why = None
    for r in fresh:
        if r.warnings:
            why = r.warnings[-1]
        elif r.plan.reason:
            why = r.plan.reason
    return False, (why or "refused by the gated port path")


def _port_group_with_companions(session, plan, blob, game_files_dir,
                                emit) -> None:
    """Port one route as a COMPANION GROUP. Reached only with the switch True.

    Everything companion-specific lives in ``companion_port``; this function is
    the thin call site the design asks for. It builds the SOURCE world's edge
    oracle from the verified bytecode decoder, looks the route's measured
    companion set up in the allowlist, and hands both to
    ``companion_port.port_group``, which is all-or-nothing.

    A missing or unparsable source ``.BEF``, and a route the allowlist does not
    describe, are REFUSALS here rather than exceptions: the run must not lose a
    whole world because one route's data is not where it was expected.
    """
    from . import befscript
    from . import companion_port
    from .bef import BefFile

    source_world = str(plan.source_world or "").upper()
    source_bef_path = Path(game_files_dir) / f"{source_world}.BEF"
    if not source_bef_path.is_file():
        session.refuse(plan, f"companion porting needs {source_bef_path.name} "
                             f"to build the spawn-edge oracle, and it is "
                             f"missing")
        return
    try:
        source_bef = BefFile.parse(source_bef_path)
    except Exception as exc:  # noqa: BLE001
        session.refuse(plan, f"companion porting needs {source_bef_path.name} "
                             f"to parse, and it does not "
                             f"({type(exc).__name__}: {exc})")
        return

    companions = companion_port.route_companions(
        plan.enemy_id, str(plan.source_world), plan.target_world)
    if companions is companion_port.ROUTE_ABSENT:
        session.refuse(plan, f"0x{plan.enemy_id:02X} "
                             f"{source_world}->{session.target_world.upper()} "
                             f"is not described by the allowlist, so its "
                             f"companion set is unknown")
        return

    oracle = companion_port.make_spawn_edge_oracle(source_bef,
                                                   befscript.REVISION_RETAIL)
    outcome = companion_port.port_group(session, plan, blob,
                                        source_bef=source_bef, edges=oracle,
                                        companions=companions)
    if outcome.ok and outcome.companion_count:
        emit(f"COMPANIONS {plan.name} 0x{plan.enemy_id:02X} "
             f"{source_world}->{session.target_world.upper()}: "
             f"{outcome.companion_count} companion(s) "
             f"{', '.join(f'0x{r.source_type_id:02X}' for r in outcome.records)}"
             f", {outcome.injected_count} injected, +{outcome.prs_growth} B "
             f".PRS / +{outcome.prt_growth} B .PRT / +{outcome.bef_growth} B "
             f".BEF")


class _PhaseMask:
    """A `SpawnConfig` seen through ONE switch pair, without copying it.

    `--cross-world` and `--cross-world-structures` stay INDEPENDENT switches.
    This wrapper is how that independence reaches `unified_admit` without
    `unified_admit` learning about CLI flags: an off switch makes that phase's
    PROJECTION empty, so the phase walks nothing and commits nothing, while the
    other phase is completely unaffected. With enemies off and structures on the
    pass still runs -- that decoupling was a deliberate earlier fix and it is
    preserved here by construction, not by a special case.

    `native_weights` is delegated UNCHANGED, including its
    `committed_routes` argument, because the retyping projection must see
    whatever actually committed.
    """

    def __init__(self, config, *, enemies_enabled: bool,
                 structures_enabled: bool):
        self._cfg = config
        self._enemies = bool(enemies_enabled)
        self._structures = bool(structures_enabled)

    def enemy_route_weights(self, world):
        if not self._enemies:
            return {}
        return self._cfg.enemy_route_weights(world)

    def structure_route_weights(self, world):
        if not self._structures:
            return {}
        # NOT re-derived: `SpawnConfig.structure_route_weights` routes through
        # `structure_routes_for_world`, which is what keeps the "switch alone
        # means yes" fallback alive. Bypassing it would silently delete that.
        return self._cfg.structure_route_weights(world)

    def native_weights(self, world, committed_routes=()):
        return self._cfg.native_weights(world, committed_routes)

    def __getattr__(self, name):
        return getattr(self._cfg, name)


def run_unified_cross_world(*, weights, seed, game_files_dir, worlds,
                            enemies_enabled: bool, structures_enabled: bool,
                            budget=None, log=None, sessions_out=None,
                            allowlist=None):
    """THE ONE ENTRY POINT (unified-randomization task 4).

    Runs BOTH admission phases per target world through `unified_admit`, over the
    REAL gated port path, and returns
    ``({filename: bytes}, {world: UnifiedResult})`` -- one record per target
    world, and the merged buffers in exactly the shape `iso_patcher` consumes.

    WHAT MAKES THIS THE REAL PASS RATHER THAN TASK 3'S DRY ONE:

      * the live size read is the SESSION's, `len(session.prs) + len(session.prt)`,
        so the budget bound is measured on committed bytes rather than predicted;
      * `port_fn` is the ACTUAL gated port path -- `session.port_enemy` (or
        `_port_group_with_companions` when `COMPANION_PORT_ENABLED`) for an enemy
        route and `port_structure_route` for a structure route. Every trust gate,
        the texture remap, the anim trust gate, the rig bind check and
        `verify_session` are untouched and undurplicated; a gate refusal becomes a
        `refused_gate` row and mutates nothing.

    ONE `WorldPortSession` per world, walked in sorted order, so the
    one-`.PRS`-resident rule holds and two routes into the same world share one
    set of buffers. A source `.BEF` is parsed per route and dropped immediately,
    keeping at most four binaries live per world.

    The two switches stay INDEPENDENT: `enemies_enabled` and
    `structures_enabled` are applied through `_PhaseMask`, and
    `structures_enabled` is additionally what `unified_admit` uses to decide
    whether phase 2 runs at all and how the `STRUCTURE_RESERVE_BYTES` slice is
    carved. A structures-only run therefore opens sessions and commits normally.
    """
    from . import roster_costs as _roster_costs
    from . import unified_draw as _ud
    from .bef import BefFile

    emit = log if log is not None else _default_log
    game_files_dir = Path(game_files_dir)

    if not MESH_PORT_ENABLED:
        for line in _PARKED_LOG_BLOCK:
            emit(line)

    masked = _PhaseMask(weights, enemies_enabled=enemies_enabled,
                        structures_enabled=structures_enabled)

    sessions: dict[str, WorldPortSession] = {}
    results: dict[str, object] = {}
    merged: dict[str, bytes] = {}

    for world in sorted(set(worlds)):
        session = WorldPortSession(world, game_files_dir, log=emit)
        sessions[world] = session

        def port_fn(cand, _session=session, _world=world):
            """Port ONE candidate. Returns `(ok, reason)`; never raises."""
            if cand.kind == "structure":
                row = _roster_costs.structure_cost_for(
                    int(cand.type_id), cand.source_world, _world)
                if row is None:
                    # Cannot happen through the oracle, and is a refusal rather
                    # than a guess if it ever does: an ESTIMATED cost is how a
                    # build silently overflows the pool.
                    return False, (f"U2 no measured roster_costs row for "
                                   f"{cand.key}, so its resident cost is "
                                   f"unknown and it is refused, never estimated")
                return port_structure_route(
                    _session, type_id=int(cand.type_id), name=cand.name,
                    source_world=cand.source_world,
                    companions=tuple(row.companions), game_files_dir=game_files_dir,
                    emit=emit)

            # --- enemy route: the UNCHANGED classification + port path -------
            # LIFTED VERBATIM into `port_enemy_route` so the arena feature can
            # reuse THIS path rather than write a second one. Same calls, same
            # order, same outcome read off `session.results`.
            return port_enemy_route(
                _session, type_id=int(cand.type_id),
                source_world=cand.source_world,
                game_files_dir=game_files_dir, allowlist=allowlist, emit=emit)

        result = _ud.unified_admit(
            world, masked, seed, structures_enabled=bool(structures_enabled),
            budget=budget, session=session, port_fn=port_fn)
        results[world] = result

        out = session.finish()
        collide = set(out) & set(merged)
        assert not collide, (f"{LOG_PREFIX} two worlds produced the same "
                             f"output filename(s): {sorted(collide)}")
        merged.update(out)

    if sessions_out is not None:
        sessions_out.update(sessions)
    return merged, results


def run_cross_world_placements(placements, *, cross_world, game_files_dir,
                               log=None, allowlist=None,
                               sessions_out=None,
                               admit_fn=None,
                               structure_fn=None,
                               structure_worlds=()) -> dict[str, bytes]:
    """Classify, port and batch every candidate placement of a run.

    ``placements`` is an iterable of ``(enemy_id, target_world, blob)`` triples:

      * ``enemy_id``     -- ``int`` enemy type id (the BEF entry's type id).
      * ``target_world`` -- lowercase world name as used by ``catalog``
                            (``"grave"``, ``"castle"``, ...). The uppercase
                            filename stem is derived from it.
      * ``blob``         -- the enemy's BEF blob bytes, taken from the SOURCE
                            world's ``.BEF``. Its mesh-index field is rewritten
                            during porting; the input bytes are never mutated.

    ONE ``WorldPortSession`` is created per target world that has at least one
    ``PORT`` placement OR is named in ``structure_worlds``, so two enemies
    going into GRAVE share one set of buffers and both survive into the output.

    KILL SWITCH: with ``MESH_PORT_ENABLED`` False this ALWAYS returns ``{}``.
    Plans are still classified and sessions are still opened (read-only) so the
    log names every route that would have been ported, but ``port_enemy``
    refuses each one before mutating anything, so every session's ``finish()``
    reports no changes and ``iso_patcher`` receives nothing to write.

    Returns ``{filename: bytes}`` merged across every world's ``finish()`` --
    exactly the shape ``iso_patcher`` consumes. Returns ``{}`` when
    ``cross_world`` is off or when nothing needed porting -- where "nothing"
    means no ``PORT`` placement AND no ``structure_worlds`` entry. ``GLOBAL.PRT`` is
    NOT consulted: texture ids in render passes are world-local, so its
    presence or absence cannot affect a port.

    ``sessions_out``, if given, is a dict that receives ``{target_world:
    WorldPortSession}`` so a caller (notably ``verify_session``) can inspect
    the in-memory buffers afterwards. Nothing here writes to disk.

    ``admit_fn``, if given, is called as ``admit_fn(plan, session)`` IMMEDIATELY
    BEFORE each group is ported, and a falsy return (or a ``(False, reason)``
    pair) SKIPS that group -- nothing is ported for it and no buffer is touched.
    It exists for ONE reason: the seeded selector's budget bound is enforced
    against the LIVE ``len(session.prs) + len(session.prt)``, and that number
    only exists in here, between two group commits. Defaults to ``None``, in
    which case this function behaves exactly as it did before the parameter
    existed -- existing callers are unaffected.

    ``admit_fn`` is a REFUSAL PATH, never a permission path: it can only ever
    stop a placement ``classify_placement`` and ``allowlist_lookup`` already
    permitted. The permission model is untouched.

    ``structure_fn``, if given, is called ONCE PER WORLD as
    ``structure_fn(session)`` after every enemy group for that world has been
    attempted and BEFORE ``session.finish()``. That position is the whole of
    Requirement 5.2 of `cross-world-structures`: the enemies are already
    admitted and committed, so the live ``len(prs) + len(prt)`` the structure
    pass reads is what the enemy pass LEFT, and a structure can only ever spend
    the remainder. It shares the session, so the one-``.PRS``-resident rule
    holds (Req 5.5).
    Defaults to ``None``, in which case this function behaves exactly as it did
    before the parameter existed.

    ``structure_worlds`` names worlds that need a session even though they have
    NO enemy placement at all. It exists because the two cross-world features
    are INDEPENDENT switches: with cross-world ENEMIES off and cross-world
    STRUCTURES on the caller passes ``placements=[]``, ``admit_fn=None`` and
    ``structure_worlds=(...)``, and each named world still gets its own session
    and its own ``structure_fn(session)`` pass. The set of sessions is the UNION
    of the enemy-target worlds and ``structure_worlds``, walked in sorted order,
    so a world with enemy placements behaves exactly as it always did --
    including the ordering rule above, structures last. ``structure_worlds`` is
    only consulted when ``structure_fn`` is given. It defaults to ``()``, in
    which case this function behaves exactly as it did before the parameter
    existed: an empty ``to_port`` still returns ``{}`` and opens nothing.
    """
    emit = log if log is not None else _default_log
    game_files_dir = Path(game_files_dir)

    if cross_world and not MESH_PORT_ENABLED:
        for line in _PARKED_LOG_BLOCK:
            emit(line)

    placements = list(placements)
    plans: list[tuple[PlacementPlan, bytes]] = []
    for enemy_id, target_world, blob in placements:
        plan = classify_placement(enemy_id, target_world,
                                  cross_world=cross_world, allowlist=allowlist)
        plans.append((plan, blob))

    native = [p for p, _ in plans if p.path == NATIVE]
    skipped = [p for p, _ in plans
               if p.path in (SKIP_TOGGLE_OFF, SKIP_NOT_ALLOWLISTED)]
    refused = [p for p, _ in plans if p.path == REFUSE_NOT_MELEE]
    to_port = [(p, b) for p, b in plans if p.path == PORT]

    for plan in skipped:
        emit(f"SKIP {plan.reason}")
    for plan in refused:
        emit(f"REFUSE {plan.reason}")
    for plan in native:
        emit(f"{plan.name} 0x{plan.enemy_id:02X} is native to "
             f"{plan.target_world.upper()}; no port needed")

    # STRUCTURES-ONLY RUNS SURVIVE THIS. `structure_worlds` names the worlds the
    # caller wants a session for regardless of enemy placements, which is what
    # makes `--cross-world-structures` independent of `--cross-world`: with the
    # enemy feature off `placements` is EMPTY BY DESIGN, and returning `{}` here
    # would mean no session was ever opened and `structure_fn` never called.
    # Empty by default, so an ordinary enemies-only caller takes the same exit it
    # always did, byte for byte.
    structure_worlds = tuple(structure_worlds or ())
    structures_requested = bool(structure_worlds) and structure_fn is not None
    if not to_port and not structures_requested:
        emit(f"SUMMARY 0 ported, {len(native)} native, {len(skipped)} skipped, "
             f"{len(refused)} refused")
        return {}
    if not to_port:
        # Said honestly: no enemy port was ASKED FOR, so "0 ported" would read
        # as a failure of something that was never attempted.
        emit(f"SUMMARY no enemy placements were requested; opening "
             f"{len(set(structure_worlds))} world session(s) for the STRUCTURE "
             f"pass only ({len(native)} native, {len(skipped)} skipped, "
             f"{len(refused)} refused)")

    # GLOBAL.PRT IS NO LONGER READ HERE. Render-pass texture ids are
    # world-local ``.PRT`` indices (module docstring: CASTLE.PRT 9-12 ==
    # ICE.PRT 21-24, byte for byte, reached by different ids from the same
    # asset), so the shared-cache boundary plays no part in the port path and
    # requiring GLOBAL.PRT would be a misleading dependency -- it aborted every
    # port for a number nothing uses. ``read_global_tex_count`` is kept in this
    # module for diagnostics, but nothing in the port path calls it.

    # Group BY TARGET WORLD -- exactly one session per world.
    by_world: dict[str, list[tuple[PlacementPlan, bytes]]] = {}
    for plan, blob in to_port:
        by_world.setdefault(plan.target_world, []).append((plan, blob))

    # THE UNION decides which sessions open: every world with an enemy placement,
    # plus every world the caller named for structures. Sorted, as before, so the
    # walk order of an enemies-only run is unchanged.
    session_worlds = sorted(set(by_world)
                            | (set(structure_worlds) if structures_requested
                               else set()))

    sessions: dict[str, WorldPortSession] = {}
    merged: dict[str, bytes] = {}
    for world in session_worlds:
        session = WorldPortSession(world, game_files_dir, log=emit)
        sessions[world] = session
        for plan, blob in by_world.get(world, ()):
            if admit_fn is not None:
                verdict = admit_fn(plan, session)
                if isinstance(verdict, tuple):
                    admitted = bool(verdict[0])
                    why = verdict[1] if len(verdict) > 1 else None
                else:
                    admitted, why = bool(verdict), None
                if not admitted:
                    # NOT a refusal and NOT a permission failure: the budget
                    # simply had no room for this group at this point in the
                    # walk. Logged with its own word so the four states of
                    # Requirement 9.2 stay distinct.
                    emit(f"NOT-ADMITTED {plan.name} 0x{plan.enemy_id:02X} "
                         f"{str(plan.source_world).upper()}->{world.upper()}: "
                         f"{why or 'admission control declined the group'}")
                    continue
            if COMPANION_PORT_ENABLED:
                # THE COMPANION ENTRY POINT, and the ONLY place the kill switch
                # is read (Requirement 2.3). With it False this branch does not
                # exist as far as the run is concerned: no companion module
                # function is called, no source .BEF is parsed for an edge oracle,
                # no graph is walked, and the else-branch below is byte-for-byte
                # the code that shipped before this feature.
                _port_group_with_companions(session, plan, blob,
                                           game_files_dir, emit)
            else:
                session.port_enemy(plan, blob)
        # DEFAULT-OFF experiment hook. Runs only for the experiment's target
        # world, and only once that world already has a session -- i.e. it rides
        # along with the ordinary allowlisted ports rather than opening a
        # session of its own.
        if (COMPANION_EXPERIMENT_TYPE_IDS
                and world == COMPANION_EXPERIMENT_TARGET_WORLD):
            _run_companion_experiment(session, game_files_dir, emit)
        # STRUCTURES GO LAST, so they can never displace a committed enemy: every
        # enemy group for this world has already been attempted and the live
        # buffer sizes the structure pass budgets against are what the enemy pass
        # left behind (Req 5.2). `None` by default -- the caller only supplies it
        # when the user asked for structures. On a structures-only run this world
        # simply had no enemy group to attempt first; the position is the same.
        if structure_fn is not None:
            structure_fn(session)
        out = session.finish()
        collide = set(out) & set(merged)
        assert not collide, (f"{LOG_PREFIX} two worlds produced the same "
                             f"output filename(s): {sorted(collide)}")
        merged.update(out)

    if sessions_out is not None:
        sessions_out.update(sessions)

    # --- end-of-run summary ------------------------------------------------
    emit("SUMMARY")
    total_placed = total_fallback = total_gate_refused = 0
    for world in sorted(sessions):
        session = sessions[world]
        placed = sum(1 for r in session.results if r.ok and not r.fallback)
        fallback = sum(1 for r in session.results if r.ok and r.fallback)
        gate_refused = [r for r in session.results if r.refused]
        world_skipped = sum(1 for p in skipped + refused
                            if p.target_world == world)
        total_placed += placed
        total_fallback += fallback
        total_gate_refused += len(gate_refused)
        emit(f"{world.upper()}: {placed} placed, {fallback} fallback, "
             f"{len(gate_refused)} refused by the trustworthiness gate, "
             f"{world_skipped} skipped")
        for result in gate_refused:
            plan = result.plan
            emit(f"GATE-REFUSED {plan.name} 0x{plan.enemy_id:02X} "
                 f"{str(plan.source_world).upper()}->{world.upper()}: "
                 f"{plan.reason}")
        # Requirement 9.4: a PLACED route reports how many companions it carried.
        # Emitted only when there are some, so a run with the kill switch off --
        # where ``companion_count`` is 0 on every result -- logs nothing extra
        # and stays line-for-line identical to today's output.
        for result in session.results:
            if not (result.ok and result.companion_count):
                continue
            plan = result.plan
            emit(f"PLACED {plan.name} 0x{plan.enemy_id:02X} "
                 f"{str(plan.source_world).upper()}->{world.upper()} with "
                 f"{result.companion_count} companion(s) "
                 f"{', '.join(f'0x{t:02X}' for t in result.companion_type_ids)} "
                 f"({result.companion_injected} injected into "
                 f"{world.upper()}.BEF)")
        for result in session.results:
            plan = result.plan
            if result.ok and plan.status == "CANDIDATE":
                emit(f"CANDIDATE {plan.name} 0x{plan.enemy_id:02X} "
                     f"{str(plan.source_world).upper()}->"
                     f"{plan.target_world.upper()} ported; confirm in game "
                     f"to promote")
    emit(f"TOTAL: {total_placed} placed, {total_fallback} fallback, "
         f"{total_gate_refused} gate-refused, "
         f"{len(skipped)} skipped, {len(refused)} refused, "
         f"{len(native)} native, {len(sessions)} world session(s), "
         f"files {sorted(merged) or 'none'}")
    return merged


# --- Buffer-only verification ----------------------------------------------

@dataclass
class VerifyReport:
    """Outcome of verifying one ``WorldPortSession``, buffer-only.

    ``ok`` is True only when every check passed AND nothing was truncated.
    """

    ok: bool = False
    requested: int = 0
    parsed: int = 0
    truncated: bool = False
    failures: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)


_CHECK_NAMES = (
    "prs_mesh_count",
    "prs_entry_bounds",
    "prs_anim_table_identical",
    "prt_structure",
    "reference_integrity",
    "blob_material_refs",
    "bef_round_trip",
    "not_truncated",
    # --- injected COMPANION blobs, held to the enemy standard (task 8.1) ---
    # Requirements 8.1-8.4. They use the SAME code paths as the enemy checks
    # above -- ``parse_material_refs`` for the refs, the final mesh count from
    # the same ``find_entry_bounds`` walk, ``read_anim_count`` for the anim
    # bound, ``BefFile.parse`` for the TOC -- because a companion blob is read
    # by the engine through exactly the same loader as an enemy blob, and
    # ``LoadBoundEntityFile`` aborts the WHOLE file if any one entry fails.
    # With no companion staged these three pass trivially over an empty list,
    # which is why turning the kill switch off leaves the report identical.
    "companion_local_mesh_refs_in_range",
    "companion_local_anim_refs_in_range",
    "companion_type_ids_unique",
)


def verify_session(session, plans=None) -> VerifyReport:
    """Verify one ``WorldPortSession`` entirely in memory.

    Every check diffs the session's live buffers against the pristine copies it
    kept from its single read. NOTHING is written to disk -- not a scratch file,
    not a ``%TEMP%`` file, nothing (Requirement 10.2/10.3).

    ``plans`` is the list of ``PORT`` plans handed to this session, used only to
    establish ``requested``. When omitted, ``requested`` falls back to the
    number of results the session recorded, so truncation cannot be detected --
    pass it whenever you have it.
    """
    import struct as _struct

    from . import prs_mesh_parser as pmp
    from .bef import (
        MATERIAL_CATEGORY_ANIM,
        MATERIAL_CATEGORY_MESH,
        BefFile,
        parse_material_refs,
    )
    from .mesh_port import find_entry_bounds
    from .texture_port import (CLUT_ENTRY_SIZE, FMT_BPP, FMT_PSMT8 as _TEX_FMT_PSMT8,
                               HEADER_SIZE as TEX_HEADER_SIZE,
                               entry_body_size as _tex_entry_body_size)

    report = VerifyReport(checks={name: True for name in _CHECK_NAMES})

    def fail(check: str, message: str) -> None:
        report.checks[check] = False
        report.failures.append(f"{check}: {message}")

    results = list(session.results)
    report.parsed = len(results)
    report.requested = len(plans) if plans is not None else len(results)
    if report.parsed < report.requested:
        # Requirement 10.8: an under-parse is a FAILURE, never a pass.
        report.truncated = True
        fail("not_truncated",
             f"TRUNCATED: {report.requested} placement(s) requested but only "
             f"{report.parsed} result(s) recorded")

    # Committed ports = results whose mesh entry was actually APPENDED to
    # session.prs (see PortResult.mesh_committed). A no-mesh fallback appends
    # nothing and must not be counted here.
    committed = [r for r in results if r.mesh_committed and r.mesh_indices]

    # COMPANIONS APPEND TOO, and their appends must be in these totals or every
    # arithmetic check below is short by exactly what they added. The session
    # companion memo is the right source: it holds one `CompanionRecord` per
    # companion actually PORTED this session -- dedupe guarantees one entry per
    # (source world, type id), a companion reused from the target `.BEF` appends
    # nothing and is never memoised as a port, and `GroupStage.restore` drops the
    # entries of any group that was rolled back. So summing it counts each append
    # exactly once and counts no append that was undone.
    #
    # This was latent until the rig-name unpacking fix: before it, every
    # companion carrying an anim record refused, and the one route that did
    # commit (`0x3A`, whose `0x35` is already in GRAVE) appends nothing at all.
    companion_records = list(getattr(session, "companion_memo", {}).values())
    injected_meshes = (sum(r.ported_meshes for r in results)
                       + sum(c.ported_meshes for c in companion_records))
    injected_tex = (sum(r.ported_textures for r in results)
                    + sum(c.ported_textures for c in companion_records))

    pristine_count = _struct.unpack_from("<I", session.pristine_prs,
                                         _MESH_COUNT_OFFSET)[0]
    final_count = _struct.unpack_from("<I", session.prs, _MESH_COUNT_OFFSET)[0]

    # --- 1. .PRS mesh count ------------------------------------------------
    # One port can append SEVERAL entries (Dark_Knight needs mesh[25] and
    # mesh[26]) but appends each DISTINCT source index only once, which is
    # exactly what ported_meshes counts.
    expected_count = pristine_count + injected_meshes
    if final_count != expected_count:
        fail("prs_mesh_count",
             f"mesh count@0x{_MESH_COUNT_OFFSET:02X} is {final_count}, "
             f"expected {pristine_count} pristine + {injected_meshes} appended "
             f"entr{'y' if injected_meshes == 1 else 'ies'} = {expected_count}")

    # --- 2. new entry bounds, inside the buffer and non-overlapping --------
    bounds: list[tuple[int, int, int]] = []
    try:
        for index in range(max(pristine_count - 1, 0), final_count):
            start, end, tag = find_entry_bounds(session.prs, index)
            bounds.append((index, start, end))
            if not (0 <= start < end <= len(session.prs)):
                fail("prs_entry_bounds",
                     f"mesh[{index}] bounds [{start}, {end}) fall outside the "
                     f"{len(session.prs)}-byte .PRS buffer")
        for (i0, s0, e0), (i1, s1, _e1) in zip(bounds, bounds[1:]):
            if s1 != e0:
                fail("prs_entry_bounds",
                     f"mesh[{i1}] starts at {s1} but mesh[{i0}] ends at {e0} "
                     f"-- entries overlap or leave a gap")
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        fail("prs_entry_bounds",
             f"bounds walk failed: {type(exc).__name__}: {exc}")

    # --- 3. Anim table after the mesh table ------------------------------
    # With nothing appended to the anim table (the substitution heuristic, and
    # every run with ANIM_PORT_ENABLED False) the region must be BYTE-IDENTICAL
    # to the pristine one, exactly as it always had to be. With records appended
    # by the PORT path the region legitimately changed, so the equality is
    # replaced by a piecewise reconstruction of what the append is allowed to
    # have done -- and nothing else.
    appended_anims_total = (sum(r.appended_anims for r in results)
                            + sum(c.appended_anims
                                  for c in companion_records))
    try:
        if final_count:
            _s, final_table_end, _t = find_entry_bounds(session.prs,
                                                        final_count - 1)
        else:
            final_table_end = _MESH_COUNT_OFFSET + 4
        if pristine_count:
            _s, pristine_table_end, _t = find_entry_bounds(
                session.pristine_prs, pristine_count - 1)
        else:
            pristine_table_end = _MESH_COUNT_OFFSET + 4
        if not appended_anims_total:
            if session.prs[final_table_end:] != session.pristine_prs[pristine_table_end:]:
                fail("prs_anim_table_identical",
                     f"the Anim table region after the mesh table differs from "
                     f"the pristine buffer (final@{final_table_end}, "
                     f"pristine@{pristine_table_end})")
        else:
            from .anim_port import find_anim_region

            before_region = find_anim_region(session.pristine_prs)
            after_region = find_anim_region(session.prs)
            if before_region is None or after_region is None:
                fail("prs_anim_table_identical",
                     "anim records were appended but the anim region cannot be "
                     "located in the pristine and/or the final .PRS")
            else:
                # (a) everything from the end of the mesh table up to and
                #     including the count word, count word excluded, verbatim.
                if (session.prs[final_table_end:after_region.anchor + 4]
                        != session.pristine_prs[
                            pristine_table_end:before_region.anchor + 4]):
                    fail("prs_anim_table_identical",
                         "the bytes between the mesh table and the anim count "
                         "word changed")
                # (b) the count word grew by EXACTLY the records appended.
                if after_region.count != before_region.count + appended_anims_total:
                    fail("prs_anim_table_identical",
                         f"the anim count word is {after_region.count}, "
                         f"expected {before_region.count} pristine + "
                         f"{appended_anims_total} appended = "
                         f"{before_region.count + appended_anims_total}")
                # (c) every pre-existing record is byte-identical and still
                #     first in the region.
                span = before_region.records_length
                if (session.prs[after_region.records_start:
                                after_region.records_start + span]
                        != session.pristine_prs[before_region.records_start:
                                                before_region.records_end]):
                    fail("prs_anim_table_identical",
                         "a pre-existing anim record changed; the port may only "
                         "append")
                # (d) the appended span is non-empty, i.e. the count word is not
                #     the only thing that moved.
                appended_span = after_region.records_end - (
                    after_region.records_start + span)
                if appended_span <= 0:
                    fail("prs_anim_table_identical",
                         f"the count word claims {appended_anims_total} appended "
                         f"record(s) but the region grew by {appended_span} "
                         f"byte(s)")
                # (e) the trailer survived verbatim (Requirement 8.4).
                if after_region.trailer != before_region.trailer:
                    fail("prs_anim_table_identical",
                         "the archive trailer after the anim records is no "
                         "longer byte-identical")
    except Exception as exc:  # noqa: BLE001
        fail("prs_anim_table_identical",
             f"anim-table comparison failed: {type(exc).__name__}: {exc}")

    # --- 4. .PRT structure -------------------------------------------------
    try:
        p_a, p_b = _struct.unpack_from("<II", session.pristine_prt,
                                       _PRT_CLUT_COUNT_OFFSET)
        f_a, f_b = _struct.unpack_from("<II", session.prt,
                                       _PRT_CLUT_COUNT_OFFSET)
        # count_a (the CLUT pool) grows once per injected INDEXED texture. A
        # direct-colour opacity-map entry carries no palette, so it adds a
        # texture chunk without adding a CLUT slot; the exact expectation is
        # therefore the number of appended INDEXED chunks, counted by the walk
        # below. Until then only the bound is enforceable.
        if not (0 <= f_a - p_a <= injected_tex):
            fail("prt_structure",
                 f"count_a@0x{_PRT_CLUT_COUNT_OFFSET:02X} grew by "
                 f"{f_a - p_a}, expected 0..{injected_tex} (one per injected "
                 f"indexed texture)")
        if f_b - p_b != injected_tex:
            fail("prt_structure",
                 f"count_b@0x{_TEX_COUNT_OFFSET:02X} grew by {f_b - p_b}, "
                 f"expected {injected_tex}")

        # Pre-existing bytes: the CLUT pool grows in the middle, so compare the
        # pristine pool as a prefix of the final pool and the pristine texture
        # stream as a prefix of the final stream.
        p_pool_end = 0x20 + p_a * CLUT_ENTRY_SIZE
        f_pool_end = 0x20 + f_a * CLUT_ENTRY_SIZE
        if session.prt[0x20:p_pool_end] != session.pristine_prt[0x20:p_pool_end]:
            fail("prt_structure", "a pre-existing CLUT slot changed")
        p_stream = session.pristine_prt[p_pool_end:]
        f_stream = session.prt[f_pool_end:]
        if not f_stream.startswith(p_stream):
            fail("prt_structure",
                 "a pre-existing texture chunk is not byte-identical to the "
                 "pristine .PRT")
        else:
            # Walk only the APPENDED chunks and size-check each one. The
            # authoritative bytes-per-pixel comes from texture_port.FMT_BPP
            # keyed on the format byte at 0x04 -- the byte at 0x05 is never
            # read by texture_port and must not be trusted (task 4.5).
            off = len(p_stream)
            appended = 0
            appended_indexed = 0
            while off < len(f_stream):
                if off + TEX_HEADER_SIZE > len(f_stream):
                    fail("prt_structure",
                         f"appended chunk {appended} header runs past the end "
                         f"of the .PRT")
                    break
                log2_w, log2_h = _struct.unpack_from("<HH", f_stream, off)
                fmt = f_stream[off + 4]
                size = _struct.unpack_from("<I", f_stream, off + 8)[0]
                if fmt not in FMT_BPP:
                    fail("prt_structure",
                         f"appended chunk {appended} has unknown format "
                         f"0x{fmt:02X}")
                    break
                expected_size = (1 << log2_w) * (1 << log2_h) * FMT_BPP[fmt]
                if size != expected_size:
                    fail("prt_structure",
                         f"appended chunk {appended} size {size} != "
                         f"(1<<{log2_w})*(1<<{log2_h})*{FMT_BPP[fmt]} = "
                         f"{expected_size}")
                    break
                flags = _struct.unpack_from("<H", f_stream, off + 6)[0]
                fields = dict(log2_w=log2_w, log2_h=log2_h, fmt=fmt,
                              flags=flags, size=size)
                if fmt == _TEX_FMT_PSMT8:
                    appended_indexed += 1
                # A non-indexed OPACITY_MAP chunk carries a further w*h alpha
                # bytes after its colour block (texture_port module docstring:
                # proven byte-exactly on all 11 .PRT files and against the
                # engine's loader at vaddr 0x001A634C-0x001A63B0). Skipping it
                # would make this walk read the next chunk's header w*h bytes
                # early and report a bogus structure failure.
                off += TEX_HEADER_SIZE + _tex_entry_body_size(fields)
                appended += 1
            if report.checks["prt_structure"] and appended != injected_tex:
                fail("prt_structure",
                     f"{appended} appended texture chunk(s) found, expected "
                     f"{injected_tex}")
            if report.checks["prt_structure"] and f_a - p_a != appended_indexed:
                fail("prt_structure",
                     f"count_a@0x{_PRT_CLUT_COUNT_OFFSET:02X} grew by "
                     f"{f_a - p_a} but {appended_indexed} of the "
                     f"{appended} appended chunk(s) are indexed and so need a "
                     f"CLUT slot each")
    except Exception as exc:  # noqa: BLE001
        fail("prt_structure", f".PRT check failed: {type(exc).__name__}: {exc}")

    # --- 5. reference integrity in the FINAL .PRS -------------------------
    # A valid render-pass texture id is a WORLD-LOCAL index into the target
    # world's own .PRT: valid iff ``id < count_b``. ``session.local_tex_count``
    # IS the final count_b (it starts at the pristine count_b and is bumped once
    # per injection). The old ceiling added GLOBAL.PRT's count on top, which
    # accepted ~164 ids that no world owns.
    ceiling = session.local_tex_count
    try:
        with pmp.parser_scope():
            reader = pmp.Reader(session.prs, _MESH_COUNT_OFFSET)
            pmp.load_mesh_res_table(reader)
            checked = [(r, i) for r in committed
                       for i in sorted(set(r.mesh_indices.values()))]
            for result, index in checked:
                prefix = f"mesh[{index}]@"
                passes = pmp.find_render_passes_under(prefix)
                # AN ID IS ONLY EVIDENCE OF A MISSED REWRITE WHEN IT CANNOT ALSO
                # BE THE RESULT OF A DONE ONE.
                #
                # This check reads FINAL bytes and asks "is this id one the remap
                # was supposed to replace?". That question is only answerable
                # when the remap's SOURCE ids and its DESTINATION ids are
                # disjoint. When they overlap, a correctly rewritten pass reads a
                # number that is also somebody's source, and the old set-based
                # test called it stale -- failing a port for doing exactly the
                # right thing. Two MEASURED ways that happened, both of which
                # discarded a whole arena that was in fact correct:
                #
                #   * IDENTITY. A source texture can land back on its own number
                #     in the target's cache, so the remap holds `33 -> 33` and a
                #     correct pass still reads 33. UNDER_B on seed 7 produced 47
                #     such "failures", every one of them expected == actual, and
                #     the message said so out loud: "still carries texture id 33,
                #     which should have been remapped to 33".
                #   * OVERLAPPING RANGES. CASTLE_K on seed 7 ported with remap
                #     `{29: 31, 30: 32, 31: 33}` -- destinations 31..33 overlap
                #     sources 29..31. A pass that legitimately became 31 (from
                #     29) was reported as an unrewritten 31 that "should have
                #     been remapped to 33". Same on seed 20260101 with 30.
                #
                # So an id is stale only if the remap meant to change it AND it is
                # not a destination of that same remap. This is deliberately
                # WEAKER than before, and the strictness it gives up is covered:
                # the `tid >= ceiling` test above is what catches the condition
                # that actually crashes a load (a dangling reference), and it is
                # untouched. What is given up is the ability to notice a missed
                # rewrite whose original id happens to collide with a
                # destination -- and that has to be given up, because the final
                # bytes do not record what the id used to be.
                remap_targets = set(result.tex_remap.values())
                stale = sorted(source_id for source_id, target_id
                               in result.tex_remap.items()
                               if source_id != target_id
                               and source_id not in remap_targets)
                for _off, tid, path in passes:
                    if tid < 0:
                        continue
                    if tid >= ceiling:
                        fail("reference_integrity",
                             f"{path} references texture id {tid} which is "
                             f">= the target .PRT's texture count "
                             f"{ceiling} -- dangling world-local texture "
                             f"reference")
                    if tid in stale:
                        fail("reference_integrity",
                             f"{path} still carries source-world-local texture "
                             f"id {tid}, which should have been remapped to "
                             f"{result.tex_remap[tid]}")
    except Exception as exc:  # noqa: BLE001
        fail("reference_integrity",
             f"re-parse of the final .PRS failed: {type(exc).__name__}: {exc}")

    # --- 6. queued blob material references -------------------------------
    # Three separate claims, all diffed against the blob as it arrived:
    #   * every LOCAL CATEGORY-1 ref now names a committed target index that is
    #     inside the final mesh table;
    #   * every ref's FLAG BYTE is unchanged, so no category and no global bit
    #     was forged;
    #   * every GLOBAL ref -- and every non-cat-1 ref -- still holds its
    #     original index, i.e. was not touched at all.
    by_enemy = {r.plan.enemy_id: r for r in results}
    companion_queue = list(getattr(session, "companion_blobs", ()))
    for type_id, blob in session.pending_blobs:
        result = by_enemy.get(type_id)
        if result is None:
            fail("blob_material_refs",
                 f"queued blob for 0x{type_id:02X} has no PortResult")
            continue
        if result.original_blob is None:
            fail("blob_material_refs",
                 f"0x{type_id:02X}: PortResult kept no original blob, so the "
                 f"rewrite cannot be verified")
            continue
        try:
            before_refs = parse_material_refs(result.original_blob)
            after_refs = parse_material_refs(blob)
        except Exception as exc:  # noqa: BLE001
            fail("blob_material_refs",
                 f"0x{type_id:02X}: material section unreadable: "
                 f"{type(exc).__name__}: {exc}")
            continue
        if len(before_refs) != len(after_refs):
            fail("blob_material_refs",
                 f"0x{type_id:02X}: {len(before_refs)} ref(s) before the "
                 f"rewrite, {len(after_refs)} after")
            continue
        if len(blob) != len(result.original_blob):
            fail("blob_material_refs",
                 f"0x{type_id:02X}: rewriting an index changed the blob length "
                 f"({len(result.original_blob)} -> {len(blob)})")
        for before, now in zip(before_refs, after_refs):
            if now.flag != before.flag:
                fail("blob_material_refs",
                     f"0x{type_id:02X}: ref at 0x{before.idx_offset:X} flag "
                     f"byte changed 0x{before.flag:02X} -> 0x{now.flag:02X}; "
                     f"category/global bit must be preserved exactly")
            local_mesh = (not before.is_global
                          and before.category == MATERIAL_CATEGORY_MESH)
            if not local_mesh:
                # A LOCAL CATEGORY-2 ref may differ from its source value ONLY
                # by having become the index this port RECORDED for it in
                # ``anim_rewrites`` (the HEURISTIC substitution, not a port).
                # Anything else -- any GLOBAL ref, any local cat-2 ref with no
                # recorded substitution, any value other than the recorded one
                # -- must still be byte-identical. The recorded index is also
                # re-bounds-checked here against the target's anim count, so
                # the check cannot be satisfied by a dangling value.
                # The PORT path records its rewrites separately, in
                # ``anim_port_rewrites`` / ``ported_anim_indices``, and is held
                # to the SAME standard: the flag byte preserved, the value equal
                # to the index the port RECORDED for that source index, and that
                # index bounds-checked against the NEW anim count. Exactly one
                # of the two dicts can carry an offset; both carrying it is
                # itself a failure, because only one branch may run per enemy.
                substituted = (None if before.is_global
                               else result.anim_rewrites.get(before.idx_offset))
                ported = (None if before.is_global
                          else result.anim_port_rewrites.get(before.idx_offset))
                if substituted is not None and ported is not None:
                    fail("blob_material_refs",
                         f"0x{type_id:02X}: ref at 0x{before.idx_offset:X} is "
                         f"recorded BOTH as a heuristic substitution and as a "
                         f"port; only one anim path may run per enemy")
                    continue
                if ported is not None:
                    port_flag, port_index = ported
                    if before.category != MATERIAL_CATEGORY_ANIM:
                        fail("blob_material_refs",
                             f"0x{type_id:02X}: ref at "
                             f"0x{before.idx_offset:X} is category "
                             f"{before.category}, but an anim PORT rewrite was "
                             f"recorded for it; only CATEGORY-2 refs may be "
                             f"repointed")
                    if port_flag != before.flag or now.flag != before.flag:
                        fail("blob_material_refs",
                             f"0x{type_id:02X}: ported anim ref at "
                             f"0x{before.idx_offset:X} did not preserve its "
                             f"flag byte (0x{before.flag:02X} -> "
                             f"0x{now.flag:02X}, recorded "
                             f"0x{port_flag:02X})")
                    if now.index != port_index:
                        fail("blob_material_refs",
                             f"0x{type_id:02X}: ported anim ref at "
                             f"0x{before.idx_offset:X} reads {now.index}, "
                             f"expected the recorded ported index "
                             f"{port_index}")
                    recorded = result.ported_anim_indices.get(before.index)
                    if recorded is None:
                        fail("blob_material_refs",
                             f"0x{type_id:02X}: ref at "
                             f"0x{before.idx_offset:X} names source anim "
                             f"{before.index}, for which no ported target "
                             f"index was recorded")
                    elif now.index != recorded:
                        fail("blob_material_refs",
                             f"0x{type_id:02X}: ported anim ref at "
                             f"0x{before.idx_offset:X} reads {now.index}, but "
                             f"source anim {before.index} was ported to "
                             f"{recorded}")
                    anim_count = session.target_anim_count()
                    if anim_count is None or not now.index < anim_count:
                        fail("blob_material_refs",
                             f"0x{type_id:02X}: ported anim index "
                             f"{now.index} is not < the NEW target anim count "
                             f"{anim_count}")
                    continue
                if substituted is None:
                    if now.index != before.index:
                        scope = "GLOBAL" if before.is_global else "non-cat-1"
                        fail("blob_material_refs",
                             f"0x{type_id:02X}: {scope} ref at "
                             f"0x{before.idx_offset:X} was modified "
                             f"({before.index} -> {now.index}); it must be left "
                             f"alone")
                    continue
                sub_flag, sub_index = substituted
                if before.category != MATERIAL_CATEGORY_ANIM:
                    fail("blob_material_refs",
                         f"0x{type_id:02X}: ref at 0x{before.idx_offset:X} is "
                         f"category {before.category}, but an anim substitution "
                         f"was recorded for it; only CATEGORY-2 refs may be "
                         f"substituted")
                if sub_flag != before.flag or now.flag != before.flag:
                    fail("blob_material_refs",
                         f"0x{type_id:02X}: substituted anim ref at "
                         f"0x{before.idx_offset:X} did not preserve its flag "
                         f"byte (0x{before.flag:02X} -> 0x{now.flag:02X}, "
                         f"recorded 0x{sub_flag:02X})")
                if now.index != sub_index:
                    fail("blob_material_refs",
                         f"0x{type_id:02X}: anim ref at 0x{before.idx_offset:X} "
                         f"reads {now.index}, expected the recorded "
                         f"substituted index {sub_index}")
                anim_count = session.target_anim_count()
                if anim_count is None or not now.index < anim_count:
                    fail("blob_material_refs",
                         f"0x{type_id:02X}: substituted anim index "
                         f"{now.index} is not < the target anim count "
                         f"{anim_count}")
                continue
            expected = result.mesh_indices.get(before.index)
            if expected is None:
                fail("blob_material_refs",
                     f"0x{type_id:02X}: local cat-1 ref naming source "
                     f"mesh[{before.index}] has no ported target index")
                continue
            if now.index != expected:
                fail("blob_material_refs",
                     f"0x{type_id:02X}: ref at 0x{before.idx_offset:X} reads "
                     f"{now.index}, expected the ported index {expected}")
            if not now.index < final_count:
                fail("blob_material_refs",
                     f"0x{type_id:02X}: ref index {now.index} is not < the "
                     f"final mesh count {final_count}")

    # --- 6b. injected COMPANION blobs (task 8.1, Requirements 8.1-8.3) -----
    # A companion has no PlacementPlan and no PortResult -- it is not placed, it
    # is SPAWNED at runtime by its root -- so the diff-against-the-original check
    # above cannot apply to it. What CAN and MUST be asserted is the property
    # that actually breaks a level: every LOCAL ref must address a slot that
    # exists in the STAGED target tables. The bound for meshes is ``final_count``
    # computed above from the same walk, and for anims it is the live anim count
    # read by the same ``read_anim_count`` the porter used.
    companion_anim_ceiling = session.target_anim_count()
    for target_type_id, blob, source_world, source_type_id in companion_queue:
        label = (f"companion 0x{source_type_id:02X} from "
                 f"{str(source_world).upper()} (injected as "
                 f"0x{target_type_id:02X})")
        try:
            refs = parse_material_refs(blob)
        except Exception as exc:  # noqa: BLE001
            fail("companion_local_mesh_refs_in_range",
                 f"{label}: material section unreadable, so the blob the engine "
                 f"would parse cannot be checked at all: "
                 f"{type(exc).__name__}: {exc}")
            continue
        for ref in refs:
            if ref.is_global:
                continue          # a GLOBAL asset exists in every world
            if ref.category == MATERIAL_CATEGORY_MESH:
                if not 0 <= ref.index < final_count:
                    fail("companion_local_mesh_refs_in_range",
                         f"{label}: LOCAL CATEGORY-1 ref at "
                         f"0x{ref.idx_offset:X} names mesh[{ref.index}], which "
                         f"is not inside the staged target mesh table "
                         f"({final_count} entries)")
            elif ref.category == MATERIAL_CATEGORY_ANIM:
                if (companion_anim_ceiling is None
                        or not 0 <= ref.index < companion_anim_ceiling):
                    fail("companion_local_anim_refs_in_range",
                         f"{label}: LOCAL CATEGORY-2 ref at "
                         f"0x{ref.idx_offset:X} names anim[{ref.index}], which "
                         f"is not inside the staged target anim table "
                         f"({companion_anim_ceiling} entries)")

    # Requirement 8.4: each injected companion type id appears EXACTLY ONCE in
    # the staged table of contents. Checked against the queue first (two
    # companions must never be handed the same id) and against the rebuilt TOC
    # below, once it exists.
    seen_companion_ids: dict[int, int] = {}
    for target_type_id, _blob, _sw, source_type_id in companion_queue:
        if target_type_id in seen_companion_ids:
            fail("companion_type_ids_unique",
                 f"target type id 0x{target_type_id:02X} was allocated to two "
                 f"companions (0x{seen_companion_ids[target_type_id]:02X} and "
                 f"0x{source_type_id:02X})")
        seen_companion_ids[target_type_id] = source_type_id

    # --- 7. BEF round-trip, progression-safe ------------------------------
    try:
        pristine_bef = BefFile.parse(session.pristine_bef)
        if ((session.pending_blobs or companion_queue)
                and session.bef == session.pristine_bef):
            session.finish()      # buffers only; finish() never writes a file
        rebuilt = BefFile.parse(session.bef)

        pristine_types = pristine_bef.type_ids()
        injected_types = {tid for tid, _ in session.pending_blobs}
        injected_types |= set(seen_companion_ids)
        # Requirement 8.4 against the REBUILT toc: exactly one entry per injected
        # companion id. ``BefFile.entries`` is one entry per TOC slot, so a
        # duplicate id would show up as a count of two here.
        for target_type_id in sorted(seen_companion_ids):
            occurrences = sum(1 for e in rebuilt.entries
                              if e.type_id == target_type_id)
            if occurrences != 1:
                fail("companion_type_ids_unique",
                     f"injected companion type id 0x{target_type_id:02X} "
                     f"appears {occurrences} time(s) in the staged .BEF table "
                     f"of contents, expected exactly 1")
        new_types = injected_types - pristine_types
        expected_entries = len(pristine_bef.entries) + len(new_types)
        if len(rebuilt.entries) != expected_entries:
            fail("bef_round_trip",
                 f"rebuilt BEF has {len(rebuilt.entries)} entries, expected "
                 f"{len(pristine_bef.entries)} pristine + {len(new_types)} "
                 f"injected = {expected_entries}")

        # Requirement 10.4: cross-world injection only APPENDS. Every type id
        # present in both files -- which includes every progression anchor,
        # every end* kill group and every PROTECTED_TAGS entry, since they are
        # all pre-existing types -- must be byte-identical, except for types we
        # deliberately injected.
        for type_id in sorted(pristine_types & rebuilt.type_ids()):
            if type_id in injected_types:
                continue
            before = pristine_bef.get_entry(type_id)
            after = rebuilt.get_entry(type_id)
            if before.data != after.data:
                fail("bef_round_trip",
                     f"pre-existing BEF entry 0x{type_id:02X} changed "
                     f"({len(before.data)} -> {len(after.data)} bytes); "
                     f"cross-world injection must only append")
        missing = pristine_types - rebuilt.type_ids()
        if missing:
            fail("bef_round_trip",
                 f"pre-existing BEF type(s) lost: "
                 f"{sorted(hex(t) for t in missing)}")
    except Exception as exc:  # noqa: BLE001
        fail("bef_round_trip",
             f"BEF round-trip failed: {type(exc).__name__}: {exc}")

    report.ok = all(report.checks.values()) and not report.truncated
    return report
