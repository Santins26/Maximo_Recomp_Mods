"""MEASURED per-route port costs (Task 4 of `randomized-enemy-roster`).

WHAT IS IN HERE
    Committed measurement data, nothing else. No cost in this file was computed,
    scaled, interpolated or guessed. Every row was produced by
    `research/tools/measure_route_costs.py`, which for ONE route opens a FRESH
    `WorldPortSession`, drives the UNMODIFIED gated path for that single group
    (`COMPANION_PORT_ENABLED` on, so a row is the WHOLE GROUP's cost), and reads
    the `.PRS` / `.PRT` / `.BEF` buffer deltas against the session's own pristine
    copies. One route per process; no game file was written.

    That harness was VALIDATED BEFORE IT WAS TRUSTED: it reproduced all six
    already-known `.PRS`/`.PRT` figures EXACTLY and reproduced Dark_Knight
    `0x53`'s known refusal, before any unknown route was measured.
    `tests/test_roster_costs.py` pins those seven so a drift fails loudly here
    rather than in game.

WHY IT IS DATA AND NOT A COMPUTATION
    Measuring the roster cannot be done inside the four-binaries-per-process
    limit in one process (Requirement 11.5), and a build must not pay for a
    dozen port measurements. So the measurement is offline and its results are
    committed. The snapshot is valid for the retail discs measured; a different
    release needs re-measuring, and the failure mode of a stale row is a SMALLER
    DRAW, never a crash, because `cost_for` returning `None` is an EXCLUSION.

A MISSING ROW IS AN EXCLUSION, NEVER A GUESS (Requirements 3.6, 3.2)
    `cost_for` returns `None` for a route with no row, and the selector must
    treat that as "not eligible" rather than substituting an estimate. That
    mechanism is unchanged and still load-bearing, but as of now ALL THIRTEEN
    eligible foreign routes into GRAVE HAVE A ROW and `UNMEASURABLE` is EMPTY.

    The last one to arrive was `0x10` Frozen_Zombie (ice). It was not freed by
    relaxing anything: the anim port was refusing a MEA node name the engine's
    own bind loop demonstrably SKIPS. See its row and the `UNMEASURABLE` note.

THE THREE ROWS THAT THE PARSER CORRECTION FREED
    `0x13` Goat_Devil used to have NO ROW, and `0x53` Dark_Knight / `0x08`
    Plant_Monster used to carry `refuses=True` with an ATTEMPT cost. All three
    were blocked by the SAME defect, and it was in the mesh walk, not in them:
    `prs_mesh_parser` read `PSTAnim`'s channel count from header +2 instead of
    +0 and `PVertexAnim`'s element count from +0 instead of +4, which desynced
    `UNDER.PRS` from entry 33 and `CASTLE.PRS` from entry 24 onward and made the
    walk resync past exactly the entries these three routes need (`UNDER`
    mesh[38]; `CASTLE` mesh[49] and mesh[54]).

    Both offsets are now MEASURED out of the engine's own loader chain
    (`research/PRS_MESH_TABLE_ENGINE_LAYOUTS.md`), the resync paths are gone --
    a desynced walk now REFUSES instead of recovering -- and all 11 `.PRS` files
    walk end to end with zero resyncs. Re-measured through the same unmodified
    harness, all three now COMMIT as a GROUP, so their rows are group costs and
    none of them carries `refuses` any more.

    NOTHING ELSE MOVED. Every route that already committed re-measures BYTE FOR
    BYTE identical (`0x0D`, `0x16`, `0x19`, `0x1D`, `0x27`, `0x3A`, `0x49`,
    `0x58`, `0x5D`), and `0x53` / `0x08` reproduce their old ATTEMPT costs
    exactly (612,154 / 100,384 and 321,824 / 264,256) on the companions-off
    path. The previously-working enemies were therefore NOT being extracted from
    the wrong bytes; the desync only ever cost the entries downstream of it.

COSTS ARE NOT ADDITIVE AND THIS FILE DOES NOT PRETEND OTHERWISE
    Requirement 3.4. A row is what the route cost ALONE into a pristine GRAVE.
    Two routes in one session share their texture and mesh memos, so a sum
    OVERSTATES. The selector uses these rows for ordering and planning only; the
    live `len(session.prs) + len(session.prt)` is the authority.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Vanilla GRAVE, the baseline every GRAVE row below was spent from.
GRAVE_VANILLA_PRS = 2_563_126
GRAVE_VANILLA_PRT = 817_232

#: The baselines the ICE and SWAMP rows were spent from. A ROW IS PER
#: (enemy, source, TARGET) and a grave row does not transfer, so each target has
#: its own vanilla pair and its own measured rows. MEASURED with `stat()`; the
#: harness asserts them in every child process, so a mis-read target is loud.
ICE_VANILLA_PRS = 3_082_301
ICE_VANILLA_PRT = 1_154_816
SWAMP_VANILLA_PRS = 3_442_632
SWAMP_VANILLA_PRT = 1_255_072

#: The baselines the UNDER and CASTLE rows were spent from (stage 2). Same rule:
#: a row is per (enemy, source, TARGET), so each target carries its own pristine
#: pair and the harness asserts it in every child process.
UNDER_VANILLA_PRS = 3_641_513
UNDER_VANILLA_PRT = 1_352_448
CASTLE_VANILLA_PRS = 4_199_541
CASTLE_VANILLA_PRT = 1_633_184

#: The FIVE worlds this data covers, i.e. every world that is a DRAW TARGET.
#: `under` and `castle` joined at stage 2, when their rows were measured; there
#: is no longer a world whose gate passes but whose costs are unknown. The
#: `_B`/`_K`/`_Q` archives are separate boss resource sets and are NOT targets.
MEASURED_TARGETS: frozenset = frozenset({"grave", "ice", "swamp",
                                         "under", "castle"})


@dataclass(frozen=True)
class RouteCost:
    """One measured route. `prs_bytes`/`prt_bytes`/`bef_bytes` are DELTAS."""

    type_id: int
    source_world: str
    target_world: str
    prs_bytes: int
    prt_bytes: int
    bef_bytes: int
    companions: tuple
    measured_note: str
    #: True when the GROUP refuses through the unmodified gated path. The bytes
    #: above are then the ATTEMPT cost (the enemy alone), and the group's actual
    #: committed cost is zero because a refusal mutates nothing.
    refuses: bool = False

    @property
    def key(self) -> tuple:
        return (self.type_id, self.source_world, self.target_world)

    @property
    def resident_bytes(self) -> int:
        """`.PRS + .PRT`, the quantity the RESOURCE pool actually bounds."""
        return self.prs_bytes + self.prt_bytes


# Note on `.BEF` deltas of -8: injecting nothing but rebuilding the TOC can make
# the file EIGHT BYTES SHORTER than vanilla (the rebuild drops trailing padding
# the original carried). It is a real measured delta and is recorded as measured;
# it is not a `.BEF` saving anyone can spend.
ROUTE_COSTS: tuple = (
    RouteCost(0x08, "castle", "grave", 564_622, 276_592, 7_280,
              (0x06, 0x09, 0x0B),
              "MEASURED, COMMITS, verify_session green. RE-MEASURED AFTER THE "
              "prs_mesh_parser FIELD-OFFSET CORRECTION (PSTAnim channel count "
              "u16@+0, PVertexAnim element count u32@+4; see "
              "research/PRS_MESH_TABLE_ENGINE_LAYOUTS.md). This row USED TO "
              "READ +321,824 .PRS / +264,256 .PRT / +3,608 .BEF with "
              "refuses=True, because companion 0x0B needs CASTLE.PRS mesh[49] "
              "-- the FIRST PVertexAnim in the file, and hence the first "
              "casualty of the +0/+4 bug -- and the walk resynced past it. "
              "CASTLE.PRS now walks 59/59 with zero resyncs, so the GROUP "
              "commits and this is the whole group's cost: the enemy's own "
              "+321,824 / +264,256 (unchanged to the byte, on the "
              "companions-off path) plus companions 0x06, 0x09 and 0x0B, all "
              "three injected"),
    RouteCost(0x0D, "swamp", "grave", 234_996, 66_576, -8,
              (),
              "MEASURED, COMMITS, verify_session green. Reproduces the known "
              "+234,996 .PRS / +66,576 .PRT exactly. Companions measured as "
              "NONE (investigated, empty -- not uninvestigated), so this is the "
              "enemy's own cost by construction"),
    RouteCost(0x10, "ice", "grave", 253_253, 239_712, 6_792,
              (0x55, 0x5C),
              "MEASURED, COMMITS, verify_session green. NEWLY MEASURABLE: this "
              "was the LAST route in UNMEASURABLE, and what freed it was an ELF "
              "MEASUREMENT, not a relaxed gate. ICE anim[41] is a MEA (tag "
              "0x0A) record whose node 0 name field holds the bytes "
              "28 00 00 00 + 28 NULs -- read as the one-character name '(' -- "
              "while the corresponding node of its own mesh[14] carries 32 NULs, "
              "i.e. NO name. 17 of its 18 node names match the ported rig "
              "exactly; that one cannot. The anim port refused the whole enemy "
              "for it. Disassembling the bind path settles it: "
              "RecurseAttachAnim__9PMeshNodeFP22PMeshNodeAnimDataTable "
              "@0x00175B20 calls Find__9PMeshNodeFPCcUi per node and, at "
              "0x00175BA8, `beql $v0, $zero` (word 0x50400006) branches PAST "
              "AttachAnim to the loop increment when the name is not found -- no "
              "CdsAssert, no error return, and the function returns 1 "
              "unconditionally at 0x00175BE4. The engine SKIPS an unmatched MEA "
              "node. Since the rig is the enemy's OWN ported mesh, byte-for-byte "
              "what ICE ships, that lookup already fails identically in VANILLA "
              "ICE, so the port changes nothing about which nodes bind. The "
              "record is appended UNCHANGED: no name substituted, no bone "
              "invented, no anim substitution heuristic, and SKA records with an "
              "unmatched bone name still REFUSE (that bind path is unmeasured). "
              "GROUP cost: companions 0x55 and 0x5C, both injected, 4 mesh "
              "entries appended (mesh 39 -> 43), 5 textures ported, anim count "
              "53 -> 62. STATUS STAYS CANDIDATE: nobody has watched it render",),
    RouteCost(0x13, "under", "grave", 442_604, 133_152, 3_544,
              (),
              "MEASURED, COMMITS, verify_session green. NEWLY MEASURABLE AFTER "
              "THE prs_mesh_parser FIELD-OFFSET CORRECTION (PSTAnim channel "
              "count u16@+0; see research/PRS_MESH_TABLE_ENGINE_LAYOUTS.md). It "
              "had NO ROW AT ALL and sat in UNMEASURABLE: UNDER.PRS resynced at "
              "entries 33-38 so mesh[38] could not be extracted (2,408 B slice, "
              "132 consumed). UNDER.PRS holds ZERO PVertexAnim records and 3 "
              "PSTAnim blocks, so that whole range was the +2/+0 channel-count "
              "bug alone; UNDER.PRS now walks 40/40 with zero resyncs. Two mesh "
              "entries appended (+442,604 .PRS), 2 textures ported (+133,152 "
              ".PRT), 10 anim records appended, anim count 53 -> 63. Companions "
              "measured as NONE, so this is the enemy's own cost by "
              "construction"),
    RouteCost(0x16, "castle", "grave", 87_263, 138_288, -8,
              (),
              "MEASURED, COMMITS, verify_session green. The cheapest .PRS route "
              "of the roster and the most .PRT-expensive per .PRS byte. "
              "Companions measured as NONE"),
    RouteCost(0x19, "swamp", "grave", 377_851, 110_656, 4_720,
              (0x1A,),
              "MEASURED, COMMITS, verify_session green. Reproduces the known "
              "+377,851 .PRS / +110,656 .PRT exactly. That is the enemy at "
              "+359,716 .PRS plus companion 0x1A at +18,135 .PRS and ZERO .PRT "
              "-- every texture 0x1A binds was already ported by the enemy and "
              "came back out of the session texture memo, which is the "
              "reuse-before-port path on the TEXTURE axis"),
    RouteCost(0x1D, "ice", "grave", 359_462, 189_536, 4_464,
              (0x1E, 0x9B, 0xBA),
              "MEASURED, COMMITS, verify_session green. Reproduces the known "
              "+359,462 .PRS / +189,536 .PRT exactly. Three companions, "
              "including the 0x1E <-> 0xBA cycle that visit-once dedupe has to "
              "survive"),
    RouteCost(0x27, "under", "grave", 509_119, 103_504, 4_784,
              (0x28,),
              "MEASURED, COMMITS, verify_session green. NEWLY MEASURED by this "
              "feature: the earlier exhaustive pass refused this route on "
              "texture_port's OPACITY_MAP-on-non-indexed case, which has since "
              "been fixed, so it now ports. The second most expensive .PRS "
              "route measured"),
    RouteCost(0x3A, "ice", "grave", 458_624, 117_808, 5_256,
              (0x35,),
              "MEASURED, COMMITS, verify_session green. Reproduces the known "
              "+458,624 .PRS / +117,808 .PRT exactly. THE REUSE-PATH CASE "
              "(Requirements 2.4, 3.5): companion 0x35 is ALREADY PRESENT in "
              "GRAVE.BEF, so the group reuses the existing entry, injects "
              "nothing, and 0x35 contributes ZERO bytes on all three axes -- "
              "this row equals the enemy-alone cost measured before companion "
              "porting existed"),
    RouteCost(0x49, "castle", "grave", 532_307, 225_472, 8_696,
              (0x5E, 0x44, 0xEB, 0x63),
              "MEASURED, COMMITS, verify_session green. Reproduces the known "
              "+532,307 .PRS / +225,472 .PRT exactly. The most expensive route "
              "of the roster: the enemy at +395,946 .PRS plus four companions, "
              "the bomb 0x5E and the blast effects 0x44 / 0xEB it itself spawns "
              "plus 0x63"),
    RouteCost(0x53, "castle", "grave", 616_642, 166_960, 4_416,
              (0x8C,),
              "MEASURED, COMMITS, verify_session green. RE-MEASURED AFTER THE "
              "prs_mesh_parser FIELD-OFFSET CORRECTION (see "
              "research/PRS_MESH_TABLE_ENGINE_LAYOUTS.md). This row USED TO "
              "READ +612,154 .PRS / +100,384 .PRT / +3,320 .BEF with "
              "refuses=True -- the long-standing Dark_Knight refusal: companion "
              "0x8C needs CASTLE.PRS mesh[54], which sat after a TOP-LEVEL "
              "resync at entry 50, so table position no longer mapped to mesh "
              "index. The resync was caused by the PSTAnim +2 read desyncing the "
              "walk inside mesh[24]; with the count read from +0 and "
              "PVertexAnim's from +4, CASTLE.PRS walks 59/59 clean and mesh[54] "
              "is addressable. The enemy-alone figure is UNCHANGED TO THE BYTE "
              "(+612,154 / +100,384 / +3,320); the extra +4,488 .PRS / +66,576 "
              ".PRT / +1,096 .BEF here is companion 0x8C"),
    RouteCost(0x58, "castle", "grave", 411_500, 84_000, 3_544,
              (),
              "MEASURED, COMMITS, verify_session green. Reproduces the known "
              "+411,500 .PRS / +84,000 .PRT exactly. Companions measured as "
              "NONE, so this is the enemy's own cost by construction"),
    RouteCost(0x5D, "under", "grave", 340_786, 198_704, 3_832,
              (),
              "MEASURED, COMMITS, verify_session green. NEWLY MEASURED by this "
              "feature, for the same reason as 0x27: the texture case that "
              "refused it before has been fixed. Companions measured as NONE"),

    # -----------------------------------------------------------------------
    # TARGET: ICE. Multi-world stage 1.
    #
    # EVERY ROW BELOW WAS MEASURED FRESH INTO ICE. None of them is a grave row
    # re-labelled: each was driven through the same unmodified
    # `research/tools/measure_route_costs.py` with `--target ice`, ONE ROUTE PER
    # PROCESS, `COMPANION_PORT_ENABLED` on, against ICE's OWN pristine
    # 3,082,301 .PRS / 1,154,816 .PRT (asserted in the child).
    #
    # THAT THE .PRS/.PRT DELTAS OFTEN EQUAL THE GRAVE ONES IS A MEASUREMENT,
    # NOT A COPY. It is also explicable: the appended bytes are the enemy's own
    # mesh entries and its world-local textures, which are properties of the
    # SOURCE, and neither target already owns them. The `.BEF` deltas are where
    # the targets diverge, because that depends on which types the target
    # already declares -- 0x0D costs -8 .BEF into grave (a TOC rebuild dropping
    # trailing padding) and +2,632 into ICE, which declares no 0x0D.
    #
    # ALL 13 ELIGIBLE FOREIGN ROUTES INTO ICE COMMIT, so `UNMEASURABLE` gains
    # nothing from this stage. Twelve verify green; 0x08 does not, and says so.
    #
    # THE FOUR FIRST-PORT ROUTES ARE 0x2F, 0x37, 0x48 and 0x6A -- GRAVE natives
    # with no measured route into anywhere, so they carried first-port risk as
    # well as measurement risk. All four COMMIT and verify green, all four
    # sourced from CASTLE: `source_candidates` sorts the mesh-owning worlds and
    # castle precedes grave, so the mesh comes from CASTLE.PRS. That is the route
    # measured and the route these rows name; the grave-sourced variant is
    # UNMEASURED and has no row.
    # -----------------------------------------------------------------------
    RouteCost(0x08, "castle", "ice", 564_622, 276_592, 7_280,
              (0x06, 0x09, 0x0B),
              "MEASURED, commits, BUT verify_session FAILS -- the ONE row of "
              "the ICE set that is not clean. The group ports and the buffers "
              "grow by exactly the figures above (identical to the grave "
              "route's, .BEF included), but reference_integrity reports "
              "appended ICE mesh[41] still carrying SOURCE-WORLD-LOCAL texture "
              "ids: id 46 where 48 was assigned, id 47 where 49 was assigned, "
              "and further ids deeper in the same node chain "
              "(mesh[41]@0x20B4D4.root.child0...). That is the texture-remap "
              "hole previously seen on 0x44 / 0x5E, and it does NOT occur for "
              "this enemy into grave, where the same route verifies clean -- so "
              "it is a per-TARGET finding, and exactly why a row is per "
              "(enemy, source, target). CONSEQUENCE: the cost is recorded and NO "
              "ALLOWLIST ENTRY WAS ADDED. With no entry the selector excludes "
              "the route for every seed, which is stronger than active=False and "
              "needs no new permission path. Nothing was relaxed to make it "
              "pass and no figure was edited"),
    RouteCost(0x0D, "swamp", "ice", 234_996, 66_576, 2_632,
              (),
              "MEASURED, COMMITS, verify_session green. Companions measured as "
              "NONE (investigated, empty). The .PRS/.PRT deltas equal the grave "
              "route's to the byte; the .BEF delta does not (-8 into grave, "
              "+2,632 here), because ICE declares no 0x0D so the blob is a real "
              "injection rather than a TOC rebuild"),
    RouteCost(0x13, "under", "ice", 442_604, 133_152, 3_544,
              (),
              "MEASURED, COMMITS, verify_session green. NOW ACTIVE BY EXPLICIT "
              "USER REQUEST, with the crash UNEXPLAINED: 0x13 Goat_Devil's "
              "OBSERVED IN-GAME CRASH is evidence about the ENEMY, not about "
              "grave, and no root cause was ever found. The user asked for it "
              "back anyway and is testing it themselves. Companions measured as "
              "NONE"),
    RouteCost(0x16, "castle", "ice", 87_263, 138_288, 1_144,
              (),
              "MEASURED, COMMITS, verify_session green. The cheapest .PRS route "
              "into ICE, as it is into grave. Companions measured as NONE"),
    RouteCost(0x19, "swamp", "ice", 377_851, 110_656, 4_720,
              (0x1A,),
              "MEASURED, COMMITS, verify_session green. Group cost: the enemy "
              "plus companion 0x1A, which ICE does not declare"),
    RouteCost(0x27, "under", "ice", 509_119, 103_504, 4_784,
              (0x28,),
              "MEASURED, COMMITS, verify_session green. Group cost with "
              "companion 0x28"),
    RouteCost(0x2F, "castle", "ice", 354_044, 250_960, 6_720,
              (0x36,),
              "MEASURED, COMMITS, verify_session green. FIRST PORT OF THIS "
              "ENEMY ANYWHERE: 0x2F Axe_Skeleton is a GRAVE native and had no "
              "measured route into any world, so this row carried first-port "
              "risk as well as measurement risk. Sourced from CASTLE, which "
              "also declares it natively. Companion 0x36 was DERIVED by "
              "`roster.derive_companions` -- the Task 1 oracle, calibrated "
              "11/11 against the catalog's hand-measured literals -- rather "
              "than hand-decoded, because no catalog entry described this route"),
    RouteCost(0x37, "castle", "ice", 239_086, 33_808, 4_936,
              (),
              "MEASURED, COMMITS, verify_session green. FIRST PORT OF THIS "
              "ENEMY ANYWHERE (0x37 Raven, a GRAVE native). THE CHEAPEST .PRT "
              "ROUTE IN THE WHOLE DATA SET at +33,808 B -- Raven is a small "
              "flyer. Companions measured as NONE"),
    RouteCost(0x48, "castle", "ice", 172_619, 117_808, 6_160,
              (0x35, 0x6A),
              "MEASURED, COMMITS, verify_session green. FIRST PORT OF THIS "
              "ENEMY ANYWHERE (0x48 Basic_Zombie, a GRAVE native). THE CHEAPEST "
              "RESIDENT ROUTE IN THE DATA SET at 290,427 B. Its derived closure "
              "contains 0x6A Torso_Zombie, itself a walking-melee enemy with its "
              "own row below: the group INJECTS it as a companion, which is not "
              "the same thing as DRAWING it, and the two rows are independent "
              "measurements"),
    RouteCost(0x53, "castle", "ice", 616_642, 166_960, 4_416,
              (0x8C,),
              "MEASURED, COMMITS, verify_session green. The most expensive "
              "route into ICE. Group cost including companion 0x8C, whose "
              "CASTLE.PRS mesh[54] is only addressable because of the "
              "prs_mesh_parser field-offset correction"),
    RouteCost(0x58, "castle", "ice", 411_500, 84_000, 3_544,
              (),
              "MEASURED, COMMITS, verify_session green. Companions measured as "
              "NONE"),
    RouteCost(0x5D, "under", "ice", 340_786, 198_704, 3_832,
              (),
              "MEASURED, COMMITS, verify_session green. Companions measured as "
              "NONE"),
    RouteCost(0x6A, "castle", "ice", 172_619, 117_808, 2_200,
              (),
              "MEASURED, COMMITS, verify_session green. FIRST PORT OF THIS "
              "ENEMY ANYWHERE (0x6A Torso_Zombie, a GRAVE native). Its "
              ".PRS/.PRT deltas equal 0x48 Basic_Zombie's exactly, which is a "
              "measurement and not a copy: 0x48's group INCLUDES 0x6A and the "
              "two share a mesh and texture set, so as a standalone route 0x6A "
              "costs the same bytes and only the .BEF delta differs (+2,200 "
              "here against 0x48's +6,160, which pays for two extra blobs). "
              "Companions measured as NONE"),

    # -----------------------------------------------------------------------
    # TARGET: SWAMP. Multi-world stage 1.
    #
    # Same harness, `--target swamp`, one route per process, against SWAMP's own
    # pristine 3,442,632 .PRS / 1,255,072 .PRT. ALL 10 ELIGIBLE FOREIGN ROUTES
    # COMMIT and all 10 verify GREEN, so nothing here goes to `UNMEASURABLE`
    # either. None of the four first-port enemies is eligible into swamp --
    # SWAMP declares 0x2F, 0x37, 0x48 and 0x6A natively -- so this set carried
    # measurement risk only.
    # -----------------------------------------------------------------------
    RouteCost(0x10, "ice", "swamp", 253_253, 239_712, 6_792,
              (0x55, 0x5C),
              "MEASURED, COMMITS, verify_session green. Group cost with "
              "companions 0x55 and 0x5C. This is the route whose ICE anim[41] "
              "MEA node name the anim port used to refuse; that tolerance is "
              "unchanged and still scoped to MEA records with at least one "
              "resolving name"),
    RouteCost(0x13, "under", "swamp", 442_604, 133_152, 3_544,
              (),
              "MEASURED, COMMITS, verify_session green. NOW ACTIVE BY EXPLICIT "
              "USER REQUEST, for the same reason as the ICE row: the OBSERVED "
              "IN-GAME CRASH recorded against 0x13 Goat_Devil is evidence about "
              "the enemy and remains UNEXPLAINED, and the user chose to ship it "
              "anyway. Companions measured as NONE"),
    RouteCost(0x16, "castle", "swamp", 87_263, 138_288, 1_144,
              (),
              "MEASURED, COMMITS, verify_session green. The cheapest .PRS route "
              "into SWAMP. Companions measured as NONE"),
    RouteCost(0x1D, "ice", "swamp", 359_462, 189_536, 4_464,
              (0x1E, 0x9B, 0xBA),
              "MEASURED, COMMITS, verify_session green. Three companions, "
              "including the 0x1E <-> 0xBA cycle visit-once dedupe has to "
              "survive, and the only route in this set that walks past an "
              "opacity-map .PRT entry (ICE.PRT entry 11)"),
    RouteCost(0x27, "under", "swamp", 509_119, 103_504, 4_784,
              (0x28,),
              "MEASURED, COMMITS, verify_session green. Group cost with "
              "companion 0x28"),
    RouteCost(0x3A, "ice", "swamp", 458_624, 117_808, 5_256,
              (0x35,),
              "MEASURED, COMMITS, verify_session green. NOTE THE CONTRAST WITH "
              "THE GRAVE ROW, which is the clearest demonstration in this file "
              "that a row does not transfer: into grave, companion 0x35 is "
              "ALREADY PRESENT in GRAVE.BEF and contributes zero on all three "
              "axes. SWAMP.BEF does not declare 0x35, so here it is a real "
              "injection -- the .PRS/.PRT figures still match because 0x35's "
              "mesh and textures came back out of the session memos, and the "
              "difference lands on .BEF"),
    RouteCost(0x49, "castle", "swamp", 532_307, 225_472, 8_696,
              (0x44, 0x5E, 0x63, 0xEB),
              "MEASURED, COMMITS, verify_session green. The most .BEF-expensive "
              "route in the data set: four companions, all absent from "
              "SWAMP.BEF. This closure was DERIVED by "
              "`roster.derive_companions` rather than read from a catalog "
              "literal, and it agrees with the grave route's hand-measured "
              "(0x5E, 0x44, 0xEB, 0x63) AS A SET -- the order differs only "
              "because the oracle returns it sorted"),
    RouteCost(0x53, "castle", "swamp", 616_642, 166_960, 4_416,
              (0x8C,),
              "MEASURED, COMMITS, verify_session green. The most expensive "
              "route into SWAMP; group cost including companion 0x8C"),
    RouteCost(0x58, "castle", "swamp", 411_500, 84_000, 3_544,
              (),
              "MEASURED, COMMITS, verify_session green. Companions measured as "
              "NONE"),
    RouteCost(0x5D, "under", "swamp", 340_786, 198_704, 3_832,
              (),
              "MEASURED, COMMITS, verify_session green. Companions measured as "
              "NONE"),

    # -----------------------------------------------------------------------
    # TARGET: UNDER. Multi-world stage 2.
    #
    # Same harness, `--target under`, ONE ROUTE PER PROCESS, against UNDER's own
    # pristine 3,641,513 .PRS / 1,352,448 .PRT (asserted in the child). All TEN
    # eligible foreign routes were driven and all ten COMMIT, so `UNMEASURABLE`
    # gains nothing here either. NINE verify green; `(0x08, "castle", "under")`
    # does not, and says so in its own row.
    #
    # MEASURED headroom against `roster_selection.pool_budget(raised=True)` =
    # 6,635,388 B: UNDER's vanilla resident total is 4,993,961 B (68%), leaving
    # 1,641,427 B, i.e. about 2-3 median arrivals. Which ones a build actually
    # gets is decided by the LIVE `len(prs) + len(prt)` at admission, not by
    # adding rows up.
    #
    # SOUND IS REFUSED FOR THIS WORLD, WHOLESALE. UNDER.BD's roundup64 occupancy
    # is 1,975,104 B, which is 9,024 B ABOVE `sound_bank.SPU_USABLE_CEILING`
    # BEFORE any import: the retail bank already fails the conservative bound, so
    # merging into it could only produce a bank that cannot load. UNDER is
    # ASSETS ONLY and its arrivals are SILENT. No ceiling was moved.
    # -----------------------------------------------------------------------
    RouteCost(0x08, "castle", "under", 564_622, 276_592, 7_280,
              (0x06, 0x09, 0x0B),
              "MEASURED, commits, BUT verify_session FAILS -- the one row of "
              "the UNDER set that is not clean, and the SAME defect this "
              "enemy shows into ICE. The group ports and the buffers grow by "
              "exactly the figures above (identical to the grave and ice "
              "routes', .BEF included), but reference_integrity reports "
              "appended UNDER mesh[40] still carrying SOURCE-WORLD-LOCAL "
              "texture ids: id 46 where 48 was assigned, id 47 where 49 was "
              "assigned, and further ids deeper in the same node chain "
              "(mesh[40]@0x2664DC.root.child0...). That is the multi-mesh "
              "texture-remap hole previously seen on 0x44 / 0x5E, and it does "
              "NOT occur into grave, where the same route verifies clean -- so "
              "it is a per-TARGET finding, which is exactly why a row is per "
              "(enemy, source, target). CONSEQUENCE: the cost is recorded and "
              "NO ALLOWLIST ENTRY WAS ADDED, which excludes the route for every "
              "seed more strongly than active=False and needs no new permission "
              "path. Nothing was relaxed and no figure was edited"),
    RouteCost(0x0D, "swamp", "under", 234_996, 66_576, 2_632,
              (),
              "MEASURED, COMMITS, verify_session green. Companions measured as "
              "NONE (investigated, empty). THE CHEAPEST RESIDENT ROUTE INTO "
              "UNDER at 301,572 B. The .PRS/.PRT deltas equal the grave, ice "
              "and swamp routes' to the byte -- a measurement, not a copy: the "
              "appended bytes are the enemy's own mesh entries and "
              "world-local textures, properties of the SOURCE, and no target "
              "already owns them. The .BEF delta is where targets diverge"),
    RouteCost(0x10, "ice", "under", 253_253, 239_712, 6_792,
              (0x55, 0x5C),
              "MEASURED, COMMITS, verify_session green. Group cost with "
              "companions 0x55 and 0x5C. The MEA-node-name tolerance this route "
              "depends on is unchanged and still scoped to MEA records with at "
              "least one resolving name; SKA records still REFUSE"),
    RouteCost(0x16, "castle", "under", 87_263, 138_288, 1_144,
              (),
              "MEASURED, COMMITS, verify_session green. The cheapest .PRS route "
              "into UNDER, as it is into every other target. Companions "
              "measured as NONE"),
    RouteCost(0x19, "swamp", "under", 377_851, 110_656, 4_720,
              (0x1A,),
              "MEASURED, COMMITS, verify_session green. Group cost: the enemy "
              "plus companion 0x1A, which UNDER.BEF does not declare"),
    RouteCost(0x1D, "ice", "under", 359_462, 189_536, 4_464,
              (0x1E, 0x9B, 0xBA),
              "MEASURED, COMMITS, verify_session green. Three companions, "
              "including the 0x1E <-> 0xBA cycle visit-once dedupe has to "
              "survive"),
    RouteCost(0x37, "castle", "under", 239_086, 33_808, 4_936,
              (),
              "MEASURED, COMMITS, verify_session green. 0x37 Raven is a GRAVE "
              "NATIVE, and this stage's stated first-port risk -- but the risk "
              "was already retired by the ICE stage, which ported it for the "
              "first time anywhere: `source_candidates` sorts the mesh-owning "
              "worlds and CASTLE precedes GRAVE, so this route sources CASTLE, "
              "the same source ICE measured. THE CHEAPEST .PRT ROUTE IN THE "
              "WHOLE DATA SET at +33,808 B, consistent with a small flyer. The "
              "grave-sourced variant remains UNMEASURED and has no row. "
              "Companions measured as NONE"),
    RouteCost(0x3A, "ice", "under", 458_624, 117_808, 5_256,
              (0x35,),
              "MEASURED, COMMITS, verify_session green. Group cost with "
              "companion 0x35, which UNDER.BEF does not declare, so unlike the "
              "grave route this is a real injection -- the .PRS/.PRT figures "
              "still match because 0x35's mesh and textures came back out of "
              "the session memos and the difference lands on .BEF"),
    RouteCost(0x53, "castle", "under", 616_642, 166_960, 4_416,
              (0x8C,),
              "MEASURED, COMMITS, verify_session green. THE MOST EXPENSIVE "
              "ROUTE INTO UNDER at 783,602 B resident, which by itself is "
              "roughly half the world's whole 1,641,427 B headroom. Group cost "
              "including companion 0x8C, addressable only because "
              "prs_mesh_parser's field offsets were corrected against the "
              "engine's loader chain"),
    RouteCost(0x58, "castle", "under", 411_500, 84_000, 3_544,
              (),
              "MEASURED, COMMITS, verify_session green. Companions measured as "
              "NONE"),

    # -----------------------------------------------------------------------
    # TARGET: CASTLE. Multi-world stage 2, and the TIGHTEST target in the game.
    #
    # Same harness, `--target castle`, one route per process, against CASTLE's
    # own pristine 4,199,541 .PRS / 1,633,184 .PRT. All EIGHT eligible foreign
    # routes were driven, all eight COMMIT and all eight verify GREEN, so
    # `UNMEASURABLE` gains nothing from this stage at all.
    #
    # AFFORDABILITY IS MEASURED HERE, NOT INFERRED FROM GRAVE. CASTLE's vanilla
    # resident total is 5,832,725 B -- 79% of the 6,635,388 B budget -- leaving
    # 802,663 B, so ONE arrival always and a SECOND only if both are cheap. The
    # two cheapest MEASURED CASTLE routes are 0x0D at 301,572 B and 0x19 at
    # 488,507 B, totalling 790,079 B against 802,663 B: a second arrival fits by
    # 12,584 B. Those are CASTLE-measured figures, not grave rows re-labelled --
    # and because costs are not additive (shared texture/mesh memos make a sum
    # an OVERSTATEMENT), the live `len(prs) + len(prt)` at admission remains the
    # authority. Every other pair overflows, so most seeds get exactly one.
    #
    # SOUND IS REFUSED FOR THIS WORLD FOR TWO INDEPENDENT REASONS, either of
    # which is fatal on its own. (1) CASTLE.BD's roundup64 occupancy is
    # 1,976,768 B, 10,688 B ABOVE `sound_bank.SPU_USABLE_CEILING` before any
    # import. (2) CASTLE.BD carries nSFX 299 against the driver's hard
    # `< 300` (`sound_bank.MAX_SFX`), so it has ZERO free entry slots and an
    # import is structurally impossible AT ANY BYTE SIZE -- reclamation cannot
    # help, because reclaiming samples frees bytes and not slots. CASTLE is
    # ASSETS ONLY and its arrivals are SILENT.
    # -----------------------------------------------------------------------
    RouteCost(0x0D, "swamp", "castle", 234_996, 66_576, 2_632,
              (),
              "MEASURED, COMMITS, verify_session green. THE CHEAPEST RESIDENT "
              "ROUTE INTO CASTLE at 301,572 B, and therefore the only route "
              "that leaves room for a second arrival in the tightest world in "
              "the game. Companions measured as NONE (investigated, empty)"),
    RouteCost(0x10, "ice", "castle", 253_253, 239_712, 6_792,
              (0x55, 0x5C),
              "MEASURED, COMMITS, verify_session green. Group cost with "
              "companions 0x55 and 0x5C"),
    RouteCost(0x13, "under", "castle", 442_604, 133_152, 3_544,
              (),
              "MEASURED, COMMITS, verify_session green. NOW ACTIVE BY EXPLICIT "
              "USER REQUEST, with the crash UNEXPLAINED: 0x13 Goat_Devil's "
              "OBSERVED IN-GAME CRASH was never explained and nothing was "
              "fixed; the enemy-level hold mechanism "
              "(`catalog.HELD_BACK_ENEMIES_ON_OBSERVED_CRASH`) is still wired "
              "and TARGET-INDEPENDENT, just empty. What this measurement adds "
              "is that the route into a "
              "FOURTH target commits with identical bytes and verifies green -- "
              "i.e. four clean structural results and one frozen console, which "
              "is evidence that the defect is NOT in the target world and NOT "
              "in anything verify_session can see. It is not an explanation, so "
              "ACTIVE ENTRIES NOW EXIST FOR THIS ENEMY IN ALL FOUR MEASURED "
              "TARGETS BY USER CHOICE, NOT BECAUSE THE DEFECT IS UNDERSTOOD. "
              "Companions measured as NONE"),
    RouteCost(0x19, "swamp", "castle", 377_851, 110_656, 4_720,
              (0x1A,),
              "MEASURED, COMMITS, verify_session green. Group cost with "
              "companion 0x1A. At 488,507 B resident it is the SECOND cheapest "
              "route into CASTLE and the other half of the only affordable pair "
              "the measurement admits"),
    RouteCost(0x1D, "ice", "castle", 359_462, 189_536, 4_464,
              (0x1E, 0x9B, 0xBA),
              "MEASURED, COMMITS, verify_session green. Three companions, "
              "including the 0x1E <-> 0xBA cycle"),
    RouteCost(0x27, "under", "castle", 509_119, 103_504, 4_784,
              (0x28,),
              "MEASURED, COMMITS, verify_session green. Group cost with "
              "companion 0x28"),
    RouteCost(0x3A, "ice", "castle", 458_624, 117_808, 5_256,
              (0x35,),
              "MEASURED, COMMITS, verify_session green. Group cost with "
              "companion 0x35. NOTE: CASTLE.BEF DOES declare 0x35, so unlike "
              "the swamp route the companion takes the REUSE path here"),
    RouteCost(0x5D, "under", "castle", 340_786, 198_704, 3_832,
              (),
              "MEASURED, COMMITS, verify_session green. Companions measured as "
              "NONE: its Script_Section carries no Spawn call at all, only a "
              "SpawnAbility that is never read as an edge"),
)

#: Eligible foreign routes into GRAVE that were DRIVEN and could not be measured
#: at all -- not as a group and not as the enemy alone. They get NO ROW, so
#: `cost_for` returns `None` and the selector excludes them (Requirement 3.6).
#: Recorded here so the exclusion is EXPLICABLE rather than an unexplained
#: absence; nothing reads this to grant permission.
UNMEASURABLE: dict = {}
#: EMPTY, and it stayed honest getting there. `(0x10, "ice", "grave")` was the
#: last entry. It was here because `ICE anim[41]` addresses one MEA node name
#: (`'('`, from the bytes `28 00 00 00` in a 32-byte name field) that the ported
#: rig does not carry. That refusal was STRICTER THAN THE ENGINE: the bind loop
#: `RecurseAttachAnim` @0x00175B20 beql-skips `AttachAnim` when
#: `PMeshNode::Find` returns NULL, so an unmatched MEA node costs one
#: un-animated node and nothing else -- and because the rig is the enemy's own
#: ported mesh, the same lookup already fails identically in vanilla ICE. The
#: tolerance is scoped to exactly that measurement: MEA records only, and only
#: when at least one name still resolves. SKA records still refuse.
#: `(0x13, "under", "grave")` LEFT this dict. It was here because UNDER.PRS's
#: mesh walk resynced at entries 33-38; that was the `PSTAnim` channel-count
#: offset bug, now corrected, and the route MEASURES AND COMMITS. Its row is
#: above. Nothing was relaxed to make it measurable -- the walk simply became
#: correct, and it is now checked harder than before (a desync REFUSES).
#:
#: THE MULTI-WORLD STAGE-1 PASS ADDED NOTHING HERE EITHER, and that is a result
#: rather than an omission: all 13 eligible foreign routes into ICE and all 10
#: into SWAMP were DRIVEN and all 23 COMMITTED. A refusal would have been
#: recorded here with its reason; none occurred. The one route with an adverse
#: finding, `(0x08, "castle", "ice")`, is NOT here: it committed and is measured,
#: so it HAS a row -- what it lacks is a `verify_session` pass, and therefore an
#: allowlist entry. This dict is only for routes with no measurable cost at all.

_BY_KEY: dict = {row.key: row for row in ROUTE_COSTS}


def cost_for(type_id: int, source_world: str, target_world: str):
    """The measured cost of one route, or `None` when it was not measured.

    `None` MEANS EXCLUDE. There is deliberately no fallback, no nearest-route
    estimate and no default: a guessed cost is how a build silently overflows
    the RESOURCE pool, and pool exhaustion produces no test failure, no build
    failure and no log line -- it ships and the game dies on level load.
    """
    return _BY_KEY.get((int(type_id), source_world, target_world))


def unmeasurable_reason(type_id: int, source_world: str, target_world: str):
    """Why a route has no row, or `None` if it is not a recorded refusal."""
    return UNMEASURABLE.get((int(type_id), source_world, target_world))


def costs_for_target(target_world: str) -> tuple:
    """Every measured row for one target world, in `type_id` order."""
    return tuple(row for row in ROUTE_COSTS
                 if row.target_world == target_world)


# ---------------------------------------------------------------------------
# STRUCTURE ROUTES (Task 4 of `cross-world-structures`)
# ---------------------------------------------------------------------------
#
# WHERE THESE NUMBERS COME FROM
#     `.kiro/specs/cross-world-structures/measurements.md`, Task 3's output, and
#     nowhere else. Nothing below was re-measured, scaled or inferred here: this
#     is data entry from that document, which recorded 64 routes in Task 3 plus
#     the 28 PHASE-2 routes measured afterwards -- 92 in all -- every one driven
#     ONE PER CHILD PROCESS through the UNMODIFIED gated port path
#     (`companion_port.port_group` -> `session.port_enemy`) followed by
#     `cross_world.verify_session`, against each target's own pristine archives.
#
# WHY A SEPARATE TABLE AND A SEPARATE LOOKUP
#     A structure route key has the SAME SHAPE as an enemy route key
#     `(type_id, source, target)`, so folding structure rows into `ROUTE_COSTS`
#     would make `cost_for` -- today's single enemy cost oracle -- start
#     returning rows for entities the enemy selector must never draw, and would
#     change existing behaviour. It is additive and behaviour-preserving to keep
#     the structure rows in their own table behind their own
#     `structure_cost_for`. `ROUTE_COSTS`, `_BY_KEY` and `cost_for` above are
#     untouched.
#
# COSTS ARE PER SOURCE, NOT PER TARGET, AND THAT IS A MEASUREMENT
#     measurements.md found each structure's `.PRS` / `.PRT` / `.BEF` deltas
#     IDENTICAL across all four of its foreign targets -- the port appends the
#     same source data whatever world receives it -- so the figures are recorded
#     once per structure and expanded per verified target below. The same
#     non-additivity caveat as the enemy rows applies: a row is what the route
#     cost ALONE, and the live `len(session.prs) + len(session.prt)` is the
#     authority.
#
# WHAT IS DELIBERATELY ABSENT
#     * `0x6C` under->grave and under->swamp: MEASURED AND REFUSED (S5,
#       surviving source-world texture ids). They get NO ROW, so
#       `structure_cost_for` returns `None` and no seed can draw them.
#     * `0x5F` Monster_Generator, excluded by measurement (GLOBAL assets).
#   Leaving 90 shippable structure routes.
#
# THE PHASE-2 SEVEN ARE NOW MEASURED, AND THEIR 28 ROWS ARE HERE.
#     `0x01` Bone_Tower, `0x02` Bonus_Grave, `0x0A` Popup_Headstone (the three
#     progression grave structures, Req 3.4), `0xAE` Underworld_Lantern, `0xB5`
#     Swamp_Coffin_Lid, `0xC8` Spirit_Statue and `0xD3` Breakable_Tiki_Torch
#     (the four Ordered_Puzzle_Props, Req 3.3) had their POLICY hold lifted with
#     NOTHING MEASURED, so Req 4.2 kept all 28 of their routes unshippable. Each
#     was then driven through the same unmodified gated path, ONE ROUTE PER
#     CHILD PROCESS, into each of its four foreign worlds: 28 routes, 28
#     COMMITTED, `verify_session` green on all 28, ZERO refusals. Those figures
#     are transcribed below and nowhere recomputed; the per-route detail is in
#     measurements.md's "Phase-2 route costs" section.
#     The home world of each of the seven was read out of the `.BEF` type tables
#     (GRAVE/ICE/SWAMP in one process, CASTLE/UNDER in a second) and agreed with
#     requirements.md's inventory table in all seven cases: GRAVE declares
#     `0x01` `0x02` `0x0A`, SWAMP declares `0xB5` `0xC8` `0xD3`, UNDER declares
#     `0xAE`, and ICE and CASTLE declare none of them.
#     MEASUREMENT IS NOT AN ANSWER TO THE OPEN QUESTIONS. It proves the assets
#     port and the target archives verify; it says nothing about whether a
#     foreign Bone Tower ever assembles, where a foreign Bonus Grave leads, or
#     whether an arrival disturbs a numbered chain. Those questions stay open in
#     `roster.STRUCTURE_HOLD_LIFTED`, and every one of the 28 rows is CANDIDATE
#     and UNOBSERVED.
#     `0x01` is the ONE structure route family that INJECTS a companion: its
#     closure resolves to `0x9E`, which is absent from all four target `.BEF`
#     files, so it is appended (inj 1). Every other structure row still measures
#     inj 0.
#
# THE POLICY HOLD ON FOUR SOURCES IS LIFTED, AND THEIR 16 ROWS ARE NOW HERE.
#     `0x17` Smashable_Snow_Pirate, `0xAD` Spirit_Torch, `0x50` Standing_Coffin
#     and `0xBC` Smashable_Coffin were withheld by Task 4 even though all 16 of
#     their routes MEASURED CLEAN and `verify_session` reported green. That was
#     a POLICY hold, never a port failure. The user asked for these structures
#     explicitly and ACCEPTED THE RISK of one misbehaving in game, so the 16
#     rows below are transcribed from measurements.md exactly like every other
#     row -- no figure here was recomputed. The reasoning, and what remains
#     UNSETTLED for each (which game var index `0x17` / `0xAD` write, and what a
#     foreign coffin DROPS for `0x50` / `0xBC`), is recorded in
#     `roster.STRUCTURE_HOLD_LIFTED`. Every one of the 16 is CANDIDATE and
#     UNOBSERVED.
#     `STRUCTURE_WITHHELD_SOURCES` is consequently EMPTY; it is kept as a table
#     so `structure_absence_reason` keeps its shape and a future hold has a
#     home.

#: The five worlds a structure may be ported INTO. Same set as
#: `MEASURED_TARGETS`; named separately so a future divergence is visible.
STRUCTURE_MEASURED_TARGETS: frozenset = MEASURED_TARGETS

#: Per-structure measured facts, transcribed from measurements.md's route table.
#: `(name, source, prs, prt, bef, meshes, anims, textures, companions,
#:   verified_targets, extra_note)`.
#: `companions` are the RESOLVED closures; `inj` was 0 for every row EXCEPT
#: `0x01` Bone_Tower's, i.e. every companion but `0x9E` was ALREADY PRESENT in
#: the target `.BEF` and was reused, so no other companion blob was added to any
#: world. `STRUCTURE_INJECTED_COMPANIONS` below carries the exception.
STRUCTURE_MEASUREMENTS: tuple = (
    (0x01, "Bone_Tower", "grave", 83_928, 36_896, 2_624, 3, 2, 2, (0x9E,),
     ("ice", "swamp", "castle", "under"),
     "PHASE-2 MEASUREMENT. The 4 routes of this once-unmeasured progression "
     "source were driven one per child process and ALL 4 COMMITTED with "
     "verify_session green. It is the ONLY structure whose companion is "
     "INJECTED rather than reused: 0x9E is absent from ICE / SWAMP / CASTLE / "
     "UNDER, so it is appended APPEND-ONLY (inj 1), which is where 1,064 of the "
     "2,624 .BEF bytes go. STILL UNSETTLED, and NOT answered by measurement: "
     "whether a foreign copy ever ASSEMBLES, since the assemble/collapse is "
     "script-driven. CANDIDATE and UNOBSERVED; see roster.STRUCTURE_HOLD_LIFTED"),
    (0x02, "Bonus_Grave", "grave", 26_676, 24_624, 2_040, 3, 2, 3,
     (0x4B, 0x67),
     ("ice", "swamp", "castle", "under"),
     "PHASE-2 MEASUREMENT. All 4 routes COMMITTED with verify_session green, "
     "both companions ALREADY PRESENT and REUSED (inj 0). STILL UNSETTLED, and "
     "NOT answered by measurement: WHERE A FOREIGN BONUS GRAVE LEADS, and -- "
     "because it carries the GameVarRead x3 + GameVarSet x1 signature that got "
     "0x17 and 0xAD held -- which game var index that GameVarSet touches and "
     "whether any world reads it. CANDIDATE and UNOBSERVED; see "
     "roster.STRUCTURE_HOLD_LIFTED"),
    (0x0A, "Popup_Headstone", "grave", 93_847, 35_872, 1_272, 3, 2, 2, (0xEF,),
     ("ice", "swamp", "castle", "under"),
     "PHASE-2 MEASUREMENT. All 4 routes COMMITTED with verify_session green "
     "through the anim PORT branch (2 records appended, 3 local category-2 "
     "refs repointed), companion 0xEF REUSED (inj 0). STILL UNSETTLED, and NOT "
     "answered by measurement: whether a foreign copy ever POPS UP, since "
     "nothing identifies the script cue -- a silent failure mode. CANDIDATE and "
     "UNOBSERVED; see roster.STRUCTURE_HOLD_LIFTED"),
    (0xAE, "Underworld_Lantern", "under", 146_852, 17_424, 2_088, 3, 3, 1,
     (0x4B, 0x67),
     ("grave", "ice", "swamp", "castle"),
     "PHASE-2 MEASUREMENT of an Ordered_Puzzle_Prop (the U_SUB3 chain member). "
     "All 4 routes COMMITTED with verify_session green, both companions REUSED "
     "(inj 0). STILL UNSETTLED, and NOT answered by measurement: whether an "
     "ARRIVAL standing outside any chain is ignored or counted by the indexing "
     "script -- if counted, the recorded failure mode is infinite loading. "
     "items.HAZARD_PROTECTED_AS_SOURCE still protects it in the ITEM PASS and "
     "is untouched. CANDIDATE and UNOBSERVED; see roster.STRUCTURE_HOLD_LIFTED"),
    (0xB5, "Swamp_Coffin_Lid", "swamp", 60_180, 67_616, 824, 3, 2, 2, (),
     ("grave", "ice", "castle", "under"),
     "PHASE-2 MEASUREMENT of an Ordered_Puzzle_Prop (an S_SUB3 chain member). "
     "All 4 routes COMMITTED with verify_session green; companions measured as "
     "NONE. Its deltas equal 0xA0 Coffin_Lid's to the byte, which is a "
     "measurement and not a copy: the two lids share a mesh/anim/texture shape. "
     "STILL UNSETTLED, and NOT answered by measurement: whether an arrival "
     "joins or disturbs the target world's chain. CANDIDATE and UNOBSERVED; see "
     "roster.STRUCTURE_HOLD_LIFTED"),
    (0xC8, "Spirit_Statue", "swamp", 140_896, 21_552, 2_024, 3, 2, 3,
     (0x4B, 0x67),
     ("grave", "ice", "castle", "under"),
     "PHASE-2 MEASUREMENT of an Ordered_Puzzle_Prop -- the S_SUB3 member "
     "measurements.md used as its known-bad CONTROL, and the source of the "
     "GameVarRead x3 + GameVarSet x1 signature that got 0x17 and 0xAD held. All "
     "4 routes COMMITTED with verify_session green, both companions REUSED "
     "(inj 0). STILL UNSETTLED, and NOT answered by measurement: the chain "
     "question, and what its persistent state does in a world that never had "
     "it. CANDIDATE and UNOBSERVED; see roster.STRUCTURE_HOLD_LIFTED"),
    (0xD3, "Breakable_Tiki_Torch", "swamp", 140_540, 17_424, 1_000, 2, 1, 1,
     (0xEF,),
     ("grave", "ice", "castle", "under"),
     "PHASE-2 MEASUREMENT of an Ordered_Puzzle_Prop (an S_SUB3 chain member). "
     "All 4 routes COMMITTED with verify_session green, companion 0xEF REUSED "
     "(inj 0). STILL UNSETTLED, and NOT answered by measurement: whether an "
     "arrival joins or disturbs the chain -- the script survey could not decide "
     "it, because the GetEntity call it makes has arity 0 and returns its own "
     "entity. CANDIDATE and UNOBSERVED; see roster.STRUCTURE_HOLD_LIFTED"),
    (0x17, "Smashable_Snow_Pirate", "ice", 183_452, 68_640, 1_880, 3, 3, 2,
     (0x4B, 0x67),
     ("grave", "swamp", "castle", "under"),
     "POLICY HOLD LIFTED at the user's explicit request, who accepted the "
     "risk. These 4 routes were measured clean in Task 3 and withheld only "
     "because this structure's Script_Section calls GameVarRead x3 + "
     "GameVarSet x1. STILL UNSETTLED: which game var index that GameVarSet "
     "touches, and whether any world reads it. CANDIDATE and UNOBSERVED; see "
     "roster.STRUCTURE_HOLD_LIFTED"),
    (0x18, "Ice_Torch", "ice", 21_152, 17_424, 1_000, 1, 1, 1, (0xEF,),
     ("grave", "swamp", "castle", "under"), ""),
    (0x1B, "Popup_Ice", "ice", 150_192, 17_424, 1_208, 2, 2, 1, (),
     ("grave", "swamp", "castle", "under"), ""),
    (0x30, "Grave_Smashable_Glass", "grave", 22_456, 17_424, 584, 2, 1, 1, (),
     ("ice", "swamp", "castle", "under"), ""),
    (0x4A, "Globe", "castle", 76_628, 153_696, 1_672, 3, 3, 6, (0x4C, 0x4F),
     ("grave", "ice", "swamp", "under"),
     "the most .PRT-expensive structure of the set, at 6 textures ported"),
    (0x50, "Standing_Coffin", "castle", 35_540, 66_576, 3_112, 2, 1, 1,
     (0x20, 0x2B, 0x45, 0x4B, 0x4E, 0x51, 0x65, 0xEF),
     ("grave", "ice", "swamp", "under"),
     "POLICY HOLD LIFTED at the user's explicit request, who accepted the "
     "risk. These 4 routes were measured clean in Task 3 and withheld only "
     "because this closure NAMES 0x2B Gold_Key and 0x65, both progression "
     "anchors -- and both were REUSED, never injected, so the port adds no "
     "anchor to any world. STILL UNSETTLED: what a foreign Standing_Coffin "
     "actually DROPS (design.md open question 2). CANDIDATE and UNOBSERVED; "
     "see roster.STRUCTURE_HOLD_LIFTED"),
    (0x6C, "Coin_Container", "under", 120_280, 45_120, 856, 2, 1, 4, (),
     ("ice", "castle"),
     "ONLY TWO OF ITS FOUR TARGETS SHIP: under->grave and under->swamp were "
     "MEASURED AND REFUSED on surviving source-world texture ids (refusal S5) "
     "and have NO ROW at all. Into ice and castle the same 4-texture set "
     "remaps cleanly, which is why a row is per (type, source, TARGET)"),
    (0x7C, "Ice_Plant", "ice", 69_124, 17_424, 2_008, 1, 5, 1, (),
     ("grave", "swamp", "castle", "under"),
     "CARRIES EXACTLY 1 INVISIBLE REF, and it is the ONLY structure that does: "
     "`companion_port.missed_local_refs` measures 1 for 0x7C and 0 for every "
     "other one of the 23 structures in all five worlds, so this is the single "
     "route family that exercises the recovered-reference path (Req 1.2). All "
     "5 anim records port and all four targets verify clean WITH that recovery "
     "in place"),
    (0xA0, "Coffin_Lid", "grave", 60_180, 67_616, 824, 3, 2, 2, (),
     ("ice", "swamp", "castle", "under"), ""),
    (0xAA, "Smashable_Door", "castle", 61_340, 9_232, 536, 2, 1, 1, (),
     ("grave", "ice", "swamp", "under"),
     "the cheapest .PRT structure route measured, at +9,232 B"),
    (0xAD, "Spirit_Torch", "castle", 112_564, 37_936, 1_928, 3, 2, 3,
     (0x4B, 0x67),
     ("grave", "ice", "swamp", "under"),
     "POLICY HOLD LIFTED at the user's explicit request, who accepted the "
     "risk. These 4 routes were measured clean in Task 3 and withheld only on "
     "the same GameVarRead x3 + GameVarSet x1 signature, plus Spawn x2. STILL "
     "UNSETTLED: which game var index that GameVarSet touches, whether any "
     "world reads it, and whether the castle torch joins a numbered chain the "
     "way its swamp sibling 0xC8 does -- which needs the level .PSX records, "
     "not the BEF blob. CANDIDATE and UNOBSERVED; see "
     "roster.STRUCTURE_HOLD_LIFTED"),
    (0xB0, "Smashable_Torch", "grave", 21_152, 17_424, 1_000, 1, 1, 1, (0xEF,),
     ("ice", "swamp", "castle", "under"),
     "its deltas equal 0x18 Ice_Torch's to the byte, which is a measurement "
     "and not a copy: the two torches share a mesh/anim/texture shape and the "
     "same reused companion 0xEF"),
    (0xB1, "Smashable_Ice_Wall", "ice", 48_844, 66_576, 536, 1, 1, 1, (),
     ("grave", "swamp", "castle", "under"), ""),
    (0xBB, "Thorn_Vine", "swamp", 181_676, 17_424, 1_752, 1, 2, 1, (),
     ("grave", "ice", "castle", "under"),
     "the most .PRS-expensive structure of the set, and the only swamp-sourced "
     "one"),
    (0xBC, "Smashable_Coffin", "castle", 30_656, 66_576, 2_968, 2, 1, 1,
     (0x20, 0x2B, 0x45, 0x4B, 0x4E, 0x51, 0x65),
     ("grave", "ice", "swamp", "under"),
     "POLICY HOLD LIFTED at the user's explicit request, who accepted the "
     "risk. These 4 routes were measured clean in Task 3 and withheld on the "
     "same closure reasoning as 0x50 Standing_Coffin -- this closure is 0x50's "
     "minus 0xEF and NAMES the same 0x2B Gold_Key and 0x65, both REUSED and "
     "never injected. STILL UNSETTLED: what a foreign Smashable_Coffin "
     "actually DROPS (design.md open question 2), asked SEPARATELY from 0x50 "
     "because the two closures are not identical. CANDIDATE and UNOBSERVED; "
     "see roster.STRUCTURE_HOLD_LIFTED"),
    (0xC9, "Breakable_Rockwall", "grave", 59_628, 5_136, 536, 2, 1, 1, (),
     ("ice", "swamp", "castle", "under"),
     "the cheapest route of the whole structure set on the .PRT axis a second "
     "time over, at +5,136 B"),
)

#: The MEASURED exception to "every companion was reused": `0x01` Bone_Tower's
#: closure resolves to `0x9E`, which no target world's `.BEF` declares, so it is
#: APPEND-ONLY injected (inj 1) into each of the four. Held as data rather than
#: written into a note by hand so the note generator below cannot claim "inj 0"
#: for a route that measured otherwise.
STRUCTURE_INJECTED_COMPANIONS: dict = {0x01: (0x9E,)}

#: Sources withheld on POLICY rather than on measurement. **NOW EMPTY.**
#: It carried `0x17`, `0xAD`, `0x50` and `0xBC` -- 16 measured-clean routes held
#: back on caution alone. The user asked for those four explicitly and accepted
#: the risk, so the hold is lifted and their rows are in
#: `STRUCTURE_MEASUREMENTS` above. The table is kept (not deleted) because
#: `structure_absence_reason` reads it and a future POLICY hold belongs here
#: rather than in a new symbol. The four lifted records live in
#: `roster.STRUCTURE_HOLD_LIFTED`; this module imports nothing from the package
#: by design, so that is a pointer and not a copy.
STRUCTURE_WITHHELD_SOURCES: dict = {}

#: Structure routes that were MEASURED AND REFUSED, so they must never ship
#: (Req 4.5). They have no cost row; this dict only explains the absence.
STRUCTURE_REFUSED: dict = {
    (0x6C, "under", "grave"):
        "S5 reference_integrity: mesh[40]@0x1F3A50.child0.mesh.frag1.layer0 "
        "still carries source-world-local texture id 38, which should have "
        "been remapped to 35. Ported, then failed verification: "
        "MEASURED-AND-REFUSED, no allowlist row, no cost row",
    (0x6C, "under", "swamp"):
        "S5 reference_integrity: "
        "mesh[42]@0x23F694.child0.child1.child0.child1.mesh.frag0.layer0 still "
        "carries source-world-local texture id 40, which should have been "
        "remapped to 42, plus at least one further surviving-id failure in the "
        "same walk. MEASURED-AND-REFUSED, no allowlist row, no cost row",
}


def _companion_clause(type_id: int, companions: tuple) -> str:
    """How this route's closure was SATISFIED, as measured, never as assumed."""
    if not companions:
        return "measured as NONE (investigated, empty)"
    injected = tuple(STRUCTURE_INJECTED_COMPANIONS.get(int(type_id), ()))
    listed = " ".join(f"0x{c:02X}" for c in companions)
    if not injected:
        return (f"resolved to {listed}, every one ALREADY PRESENT in the target "
                f".BEF and REUSED (inj 0), so no companion blob was added")
    inj_list = " ".join(f"0x{c:02X}" for c in injected)
    reused = tuple(c for c in companions if c not in injected)
    clause = (f"resolved to {listed}, of which {inj_list} was NOT present in "
              f"the target .BEF and was APPEND-ONLY INJECTED (inj "
              f"{len(injected)})")
    if reused:
        clause += (", the rest ALREADY PRESENT and REUSED"
                   f" ({' '.join(f'0x{c:02X}' for c in reused)})")
    return clause


def _structure_route_costs() -> tuple:
    """Expand the per-structure facts into one `RouteCost` per shipped route.

    EXPANSION, NOT COMPUTATION: measurements.md measured the deltas to be equal
    across a structure's four targets, so each row carries the measured figures
    verbatim and differs only in `target_world`. A target absent from a
    structure's `verified_targets` gets no row at all.
    """
    rows = []
    for (type_id, name, source, prs, prt, bef, meshes, anims, textures,
         companions, targets, extra) in STRUCTURE_MEASUREMENTS:
        for target in targets:
            note = (
                f"STRUCTURE ROUTE, MEASURED, COMMITS, verify_session green "
                f"(Task 3, measurements.md). 0x{type_id:02X} {name} "
                f"{source} -> {target}: +{prs:,} .PRS / +{prt:,} .PRT / "
                f"+{bef:,} .BEF, {meshes} mesh entr"
                f"{'y' if meshes == 1 else 'ies'} / {anims} anim record"
                f"{'' if anims == 1 else 's'} / {textures} texture"
                f"{'' if textures == 1 else 's'} ported. Companions "
                + _companion_clause(type_id, companions)
                + ". Ported through the anim PORT branch, never the "
                  "substitution arm")
            if extra:
                note += f". {extra}"
            rows.append(RouteCost(type_id, source, target, prs, prt, bef,
                                  tuple(companions), note))
    return tuple(rows)


#: One measured row per SHIPPABLE structure route: 90 of the 92 routes measured
#: in all -- the 62 of Task 3's 64 that committed and verified, plus the 28
#: PHASE-2 routes of `0x01` / `0x02` / `0x0A` / `0xAE` / `0xB5` / `0xC8` / `0xD3`,
#: which all 28 committed and verified. The only absences are still the two
#: MEASURED-AND-REFUSED `0x6C` routes. Keyed and shaped exactly like
#: `ROUTE_COSTS`, kept separate so the enemy oracle above is untouched.
STRUCTURE_ROUTE_COSTS: tuple = _structure_route_costs()

#: 90, asserted at import so a transcription slip is loud rather than a silently
#: shorter table. Was 46; the four lifted POLICY holds added 16 (-> 62) and the
#: seven newly MEASURED phase-2 sources add 28 more.
STRUCTURE_ROUTE_COST_COUNT: int = 90
assert len(STRUCTURE_ROUTE_COSTS) == STRUCTURE_ROUTE_COST_COUNT, (
    f"expected {STRUCTURE_ROUTE_COST_COUNT} measured structure route rows, "
    f"got {len(STRUCTURE_ROUTE_COSTS)}")

_STRUCTURE_BY_KEY: dict = {row.key: row for row in STRUCTURE_ROUTE_COSTS}


def structure_cost_for(type_id: int, source_world: str, target_world: str):
    """The measured cost of one STRUCTURE route, or `None` to EXCLUDE it.

    `None` means exclude, with no fallback and no estimate, exactly as
    `cost_for` does for enemies (Req 4.2). It returns `None` for the two
    measured-and-refused `0x6C` routes, for every route whose source is
    withheld, for `0x5F`, and for anything unmeasured.
    """
    return _STRUCTURE_BY_KEY.get((int(type_id), source_world, target_world))


def structure_absence_reason(type_id: int, source_world: str,
                             target_world: str):
    """Why a structure route has no cost row, when that is recorded.

    Reporting only; nothing grants permission from here.
    """
    key = (int(type_id), source_world, target_world)
    if key in STRUCTURE_REFUSED:
        return STRUCTURE_REFUSED[key]
    return STRUCTURE_WITHHELD_SOURCES.get(int(type_id))


def structure_costs_for_target(target_world: str) -> tuple:
    """Every shippable structure row for one target world, in `type_id` order."""
    return tuple(sorted(
        (row for row in STRUCTURE_ROUTE_COSTS
         if row.target_world == target_world),
        key=lambda row: row.type_id))
