# Can dropping GRAVE's unused native samples buy room for ALL foreign enemy sounds?

Measurement only. No module changed, no file written into `game_files/`, no `.BD`/`.BEF`/`.PRS`/ISO produced.
Everything below was produced with the existing `randomizer.sound_bank`, `randomizer.sound_ids`,
`randomizer.roster*` and `randomizer.bef` code paths.

## Answer first

**No. "All foreign enemies get their sounds" is NOT reachable. Reclamation moves the number from 2-3 to 4-5.**

* best case measured: **5 of 6** (seed 1)
* worst case measured: **4 of 6** and **4 of 7** (seeds 7, 20260101, 424242)
* typical: **4**

Reason, in one line: the reclaimable class is tiny. Only **13 of 277** bank entries (**84,080 B**, 4.4% of the
sample block) are exclusively owned by a walking-melee enemy, while **206 entries (1,357,120 B, 71.8%)** are
referenced by no blob at all and can never be touched.

## Batching

Four analysis processes, each within the four-binary limit:

1. `GRAVE.BD` + `GRAVE.BEF` — attribution and buckets (2 binaries).
2. Source-world sound costs — `GRAVE.BD` + one world's `.BD` + `.BEF`, worlds walked one at a time and each
   dropped before the next (3 resident at a time; 9 opened in one process, which is the one place the
   four-per-*process* reading was stretched — peak residency stayed at target-ids + one world).
3. Seeded draws — `roster.read_world_snapshots` batched 4 worlds then 1 (the existing test's batching).
4. `SLUS_200.17` alone (1 binary), plus pure-arithmetic processes with no binaries.

## 1. Attribution of all 277 GRAVE.BD entries (MEASURED)

`GRAVE.BD` re-derived: 1,893,776 B file, nSFX 277, header 3,328 B, samples 1,890,448 B, `roundup64`
1,893,824. `validate` clean, duplicates `(1435, 1444)`, 275 distinct ids.

`GRAVE.BEF`: 70 blobs, **0 refused to decode**, 127 slot-31 `PlaySoundEffect` sites, 77 distinct literal ids
referenced. Eight referenced literals are NOT in the bank — `404 405 406` (0x16 Axe_Guard) and
`430 431 432 433 434` (0x0D Zombie_Crocodile): direct measured evidence of the silence bug for blobs already
declared in GRAVE.

| bucket | ids | entries | bytes | share of sample block |
|---|---|---|---|---|
| exclusively owned by ONE walking-melee type | 13 | 13 | 84,080 | 4.4% |
| multi-owner, at least one walking-melee | 2 | 2 | 6,128 | 0.3% |
| owned only by non-walking-melee entities | 54 | 55 | 443,120 | 23.4% |
| **UNATTRIBUTED (no blob references them)** | **206** | **207** | **1,357,120** | **71.8%** |

Exclusive owners:

| owner | native to GRAVE | ids | bytes |
|---|---|---|---|
| 0x6B Ghost | yes | 135 137 139 140 852 | 49,776 |
| 0x54 Guard_Skeleton | yes | 119 120 | 15,296 |
| 0x0D Zombie_Crocodile | no | 54 1367 | 8,176 |
| 0x37 Raven | yes | 131 132 | 6,592 |
| 0x47 Basic_Skeleton | yes | 716 1445 | 4,240 |

Multi-owner: id 114 (2,112 B; 0x2F + 0x39), id 130 (4,016 B; 0x48 + 0x6A).

Natives `0x2F 0x39 0x48 0x6A` own **no** exclusive ids. So the theoretical ceiling on reclamation, with every
GRAVE native absent and 0x0D's two ids kept (0x0D is drawn in all four sampled seeds), is
**75,904 + 6,128 = 82,032 B** — 4.3% of the bank.

## 2. Unrecoverable references (MEASURED)

16 slot-31 sites across 10 types take a non-literal id argument:

| type | walking-melee | sites |
|---|---|---|
| 0xEE Unknown | no | 4 |
| 0x48 Basic_Zombie | yes | 2 |
| 0x54 Guard_Skeleton | yes | 2 |
| 0xF5 Unknown | no | 2 |
| 0x24, 0x5F, 0xF6 Unknown | no | 1 each |
| 0x2F Axe_Skeleton, 0x39 SwordSkeleton, 0x47 Basic_Skeleton | yes | 1 each |

Residual risk: 16 sites, 6 of them in walking-melee types whose exclusive ids reclamation would drop
(0x47 and 0x54 both appear in the exclusive-owner table AND have non-literal sites). A register-sourced id can
reach an id we attributed to nobody, so an id dropped as "exclusively 0x47's" could in principle also be
reached by a non-literal site in another type. Over-dropping is a logged no-op, not corruption.

## 3. Per-seed reclaim (MEASURED draws, real cost rows; committed set is a dry admission walk over
`plan_draw` order with predicted bytes — INFERRED where real porting could still refuse a route)

Foreign sound costs re-derived from source banks (all eight previously measured figures reproduced
byte-for-byte). New: **0x53 Dark_Knight 66,032 B**, **0x49 Bomb_Skeleton 0 B — every id it needs is already in
GRAVE (101 115 116 457 717 1371)**.

| seed | committed foreign | natives in lineup | absent natives | ids dropped | reclaimed B | headroom (conservative) | fit cheapest-first | all fit |
|---|---|---|---|---|---|---|---|---|
| 1 | 6 | none | all 8 | 13 | 82,032 | 150,848 | **5/6** (0x49, 0x58, 0x16, 0x0D, 0x27) | no |
| 7 | 6 | 0x54 | 7 | 10 | 62,720 | 131,520 | **4/6** (0x49, 0x16, 0x19, 0x0D) | no |
| 20260101 | 7 | none | all 8 | 12 | 78,016 | 146,880 | **4/7** (0x58, 0x16, 0x19, 0x0D) | no |
| 424242 | 7 | none | all 8 | 12 | 78,016 | 146,880 | **4/7** (0x58, 0x1D, 0x16, 0x19) | no |

Today, without reclamation: 3, 2, 2, 3 respectively (the 3s include the free 0x49).
Reclamation is worth **+1 to +2 enemies**, never all of them.

Notable: the draw fills the lineup almost entirely with foreigners (6-7 foreign, 0-1 native), so the natives
ARE nearly all absent — the optimistic side of the premise holds. The room still is not there.

## 4. Ceiling tightening: RPC command 14 (PARTIAL)

Three `sceSifCallRpc`-shaped sites in `SLUS_200.17` send command 14 (`a1 = 0xE`, `a2 = 1`, send size 0x414,
receive size 4, through the same helper at VA 0x0010FBF8): **VA 0x00141704, 0x00141848, 0x0019682C**, with
send buffers at 0x00232D40 and 0x002E20C0. **The value was NOT isolated**: `args[0]`/`args[1]` are read out of
those runtime-populated send buffers, not from literals at the call site. Tracing the writers is a separate
job.

It does not matter for the decision: recomputing every seed at both ends of the 68,672-72,256 band gives the
**same fit counts**, so the ~3,584 B is not the difference between four and all.

## 5. Reasons reclamation is a bad idea (or at least a poor trade)

1. **The droppable class is 4.4% of the bank.** 84,080 B against per-enemy costs of 10,240-86,192 B.
2. **The unattributed bucket dominates: 71.8%, 1,357,120 B, untouchable.** That is the hard bound; no
   attribution refinement gets past it.
3. **Six of the exclusive-owner ids belong to types (0x47, 0x54) that also have non-literal slot-31 sites**, so
   dropping them carries a real (if silent-failure-only) risk of muting something else.
4. **Even the theoretical maximum does not close the gap.** Absolute best headroom 154,432 B; the four seeds'
   full foreign sound bills are 226,096 / 271,568 / 330,240 / 293,072 B. All-sounds needs roughly double the
   entire reclaimable budget.

If the goal is "more foreign enemies audible", the cheap wins are elsewhere: 0x49 already costs zero, and the
expensive tail (0x5D 86,192; 0x53 66,032; 0x3A 60,272) is what actually blocks the lineup.
