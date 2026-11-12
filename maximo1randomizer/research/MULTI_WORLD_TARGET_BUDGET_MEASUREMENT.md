# Multi-world arrival budgets — measurement pass

MEASUREMENT ONLY. No module was changed, no allowlist entry added, no artifact
written. Every number below is either MEASURED (produced by running the shipped
modules / reading the shipped binaries) or INFERRED (labelled as such).

Batching, as required: `.PRS`/`.PRT` sizes were taken with `stat()` only (no
bytes read). Mesh-table walks were run in three processes — {grave, castle,
swamp}, {ice, under}, {CASTLE_K, CASTLE_Q, ICE_B, SWAMP_B} — plus one for
UNDER_B alone. `.BD` banks were parsed in one process of four
{CASTLE, UNDER, ICE, SWAMP}. `.BEF` snapshots were read in the two batches the
`roster` module itself mandates (4 + 1). `SLUS_200.17` was read in its own
processes.

---

## 1. RESOURCE pool headroom per target world

MEASURED constants: `elf_patch.RESOURCE_POOL_RAISED` = 7,340,032
(`RESOURCE_POOL_VANILLA` = 6,553,600), `roster_selection.LOADED_MAX_PCT` = 90.4,
`roster_selection.pool_budget(raised=True)` = **6,635,388 B**.

MEASURED vanilla archive sizes and the resident total the pool bounds
(`len(.PRS) + len(.PRT)`):

| world (main archive) | .PRS | .PRT | resident sum | % of raised pool | % of vanilla pool | headroom vs 6,635,388 |
|---|---|---|---|---|---|---|
| GRAVE (control) | 2,563,126 | 817,232 | **3,380,358** | 46.05% | 51.58% | **3,255,030** |
| CASTLE | 4,199,541 | 1,633,184 | **5,832,725** | 79.46% | 88.98% | **802,663** |
| SWAMP | 3,442,632 | 1,255,072 | **4,697,704** | 64.00% | 71.68% | **1,937,684** |
| ICE | 3,082,301 | 1,154,816 | **4,237,117** | 57.73% | 64.65% | **2,398,271** |
| UNDER | 3,641,513 | 1,352,448 | **4,993,961** | 68.04% | 76.20% | **1,641,427** |

Note on the control: GRAVE's 51.6% figure in the brief is a percentage of the
VANILLA pool. Against the RAISED pool it is 46.05%. Both are given above.

Boss-arena archives (see §1b), for completeness:

| archive | .PRS | .PRT | sum | headroom vs budget |
|---|---|---|---|---|
| CASTLE_K | 2,285,212 | 823,728 | 3,108,940 | 3,526,448 |
| CASTLE_Q | 1,555,080 | 219,264 | 1,774,344 | 4,861,044 |
| SWAMP_B | 1,795,897 | 455,040 | 2,250,937 | 4,384,451 |
| ICE_B | 2,409,691 | 601,744 | 3,011,435 | 3,623,953 |
| UNDER_B | 2,783,901 | 448,048 | 3,231,949 | 3,403,439 |
| GLOBAL | 8,715,968 | 2,820,704 | 11,536,672 | (not pool-resident on this budget) |

### Yardstick — MEASURED GRAVE route costs

All 13 `roster_costs.ROUTE_COSTS` rows target grave. Resident cost = prs + prt:

| route | src | prs | prt | resident |
|---|---|---|---|---|
| 0x16 | castle | 87,263 | 138,288 | 225,551 |
| 0x0D | swamp | 234,996 | 66,576 | 301,572 |
| 0x19 | swamp | 377,851 | 110,656 | 488,507 |
| 0x10 | ice | 253,253 | 239,712 | 492,965 |
| 0x58 | castle | 411,500 | 84,000 | 495,500 |
| 0x5D | under | 340,786 | 198,704 | 539,490 |
| 0x1D | ice | 359,462 | 189,536 | 548,998 |
| 0x13 | under | 442,604 | 133,152 | 575,756 |
| 0x3A | ice | 458,624 | 117,808 | 576,432 |
| 0x27 | under | 509,119 | 103,504 | 612,623 |
| 0x49 | castle | 532,307 | 225,472 | 757,779 |
| 0x53 | castle | 616,642 | 166,960 | 783,602 |
| 0x08 | castle | 564,622 | 276,592 | 841,214 |

MEASURED: min 225,551 · **median 548,998** · mean 556,922 · max 841,214.

### Affordable arrivals (INFERRED from the grave yardstick)

| target | headroom | at median (548,998) | at cheapest-first |
|---|---|---|---|
| GRAVE | 3,255,030 | ~5–6 | up to 14 |
| ICE | 2,398,271 | **4** | up to 10 |
| SWAMP | 1,937,684 | **3** | up to 8 |
| UNDER | 1,641,427 | **2–3** | up to 7 |
| CASTLE | 802,663 | **1** | **2** (its cheapest eligible foreign routes are 0x0D 301,572 + 0x19 488,507 = 790,079, which fits with 12,584 B to spare; a third does not fit) |

Caveat, explicit: a cost row is per (enemy, source, TARGET). None of these
numbers is a cost into castle/swamp/ice/under. They are the only empirical
distribution available and are used here as a scale, not as a prediction.

### 1b. Is the budget unit per-WORLD or per-LEVEL?

**Per RESOURCE SET, i.e. per level group — and the `_B`/`_K`/`_Q` archives are
separate sets with their own budget, not additional load on the main one.**

MEASURED evidence:

- `elf_patch.find_level_table(SLUS_200.17)` → table at file 0xCEFC0, **31 rows**,
  stride 0x34. Every row's `+0x00` names a `.PSX` level and `+0x08` names only
  the world FOLDER (`PSXDATA\CASTLE\`, `PSXDATA\ICESHIP\`, …). No row names a
  `.PRS`/`.PRT`. Rows group into contiguous per-world blocks:
  GRAVE [0–5]+[30], SWAMP [6–11], ICESHIP [12–17], UNDER [18–23],
  CASTLE [24–29] (with `C_KING` at 28 and `C_QUEEN` at 29).
- MEASURED: the executable contains no `WORLD.PRS` string at all. The only
  literal archive paths are `PSXDATA\GAME\GLOBAL.PRS/PRT`,
  `PSXDATA\GAME\FONT.PRS/PRT`, `PSXDATA\GAME2\GALLERY.PRS/PRT`. World archive
  names are built at runtime from `"%s%s"` + `"PRS"`/`"PRT"` (both format and
  extension literals present, at 0x0022BC58/0x0022BC60/0x0022BC64). So the
  level table cannot be read to name a world's archive directly.
- The decisive file-set fact: **`GRAVE_B.BEF` exists with no `GRAVE_B.PRS` and no
  `GRAVE_B.PRT`.** Every other boss set (`SWAMP_B`, `ICE_B`, `UNDER_B`,
  `CASTLE_K`, `CASTLE_Q`) ships a full `.BEF`+`.PRS`+`.PRT` triple. That is only
  consistent with: a boss arena is its own resource set, loaded INSTEAD OF the
  main world archive, and grave's boss arena is the one that reuses the main
  archive (hence a `_B` BEF but no `_B` archive).
- Corroborating: each boss row carries its own sound-bank selector at `+0x13`
  (grave boss 0xDA, swamp boss 0xD8, ice boss 0xD9, under boss 0xD7, king 0xD5,
  queen 0xD6 — matching the shipped `BS_GRAVE/BS_SWAMP/BS_ICE/BS_UNDER/BS_KING/
  BS_QUEEN.BD`), while all normal rows carry the world bank (0xD0–0xD4).

Consequence for this question: a cross-world arrival that should appear during
normal play belongs in the MAIN archive, and the per-world table in §1 is the
right budget. The `_B`/`_K`/`_Q` numbers in the second table are separate,
independently-budgeted levels; their much larger headroom is not available to
normal-play arrivals.

---

## 2. Eligible foreign roster per target world

MEASURED via `roster.build_roster` (BEF snapshots read 4 + 1) then
`roster.eligible_foreign` / `roster.eligible_native`.

`roster.classification_counts` = {enemy 21, companion_only 42, non_enemy 45,
unclassified 33}.

Home worlds, MEASURED from the native lists: castle {0x08, 0x16, 0x49, 0x53,
0x58} · swamp {0x0D, 0x19} · ice {0x10, 0x1D, 0x3A} · under {0x13, 0x27, 0x5D} ·
grave {0x2F, 0x37, 0x39, 0x47, 0x48, 0x54, 0x6A, 0x6B}.

| target | foreign eligible | type ids | natives | of the foreign, ids with an existing grave cost row |
|---|---|---|---|---|
| grave (control) | 13 | 0x08 0x0D 0x10 0x13 0x16 0x19 0x1D 0x27 0x3A 0x49 0x53 0x58 0x5D | 8 | 13 of 13 |
| castle | **8** | 0x0D 0x10 0x13 0x19 0x1D 0x27 0x3A 0x5D | 13 | **8 of 8** |
| swamp | **10** | 0x10 0x13 0x16 0x1D 0x27 0x3A 0x49 0x53 0x58 0x5D | 11 | **10 of 10** |
| ice | **13** | 0x08 0x0D 0x13 0x16 0x19 0x27 0x2F 0x37 0x48 0x53 0x58 0x5D 0x6A | 8 | **9 of 13** (0x2F 0x37 0x48 0x6A are grave natives — never ported anywhere) |
| under | **10** | 0x08 0x0D 0x10 0x16 0x19 0x1D 0x37 0x3A 0x53 0x58 | 11 | **9 of 10** (0x37 is a grave native) |

Total new (enemy, target) route pairs to measure for the four worlds: **41**.
Of those, 36 involve an enemy that has already ported cleanly into grave once —
which is evidence about the enemy, not a transferable cost. Five pairs
(0x2F, 0x37, 0x48, 0x6A into ice; 0x37 into under) involve enemies with no
measured route at all, so they carry both measurement AND first-port risk.

---

## 3. Target-gate status, re-confirmed

MEASURED `cross_world.WorldPortSession(world, game_files).target_trust()`:

| world | ok | resync events | declared / walked entries | walk end | anim anchor | end == anchor |
|---|---|---|---|---|---|---|
| grave | **True** | 0 | 39 / 39 | 1,984,784 | 1,984,784 | yes |
| castle | **True** | 0 | 59 / 59 | 2,834,628 | 2,834,628 | yes |
| swamp | **True** | 0 | 42 / 42 | 2,356,884 | 2,356,884 | yes |
| ice | **True** | 0 | 41 / 41 | 2,143,444 | 2,143,444 | yes |
| under | **True** | 0 | 40 / 40 | 2,516,188 | 2,516,188 | yes |

All five pass with zero resyncs and exact anchor agreement. The technical gate
is not a blocker for any of the four.

Variant archives, MEASURED via `mesh_port.assess_target_table` (they are not
addressable as `WorldPortSession` targets, which names files `WORLD.PRS`):

| archive | ok | resyncs | walked | walk end | anchor | shortfall |
|---|---|---|---|---|---|---|
| CASTLE_K | False | 0 | 22 / 22 | 1,153,184 | 1,313,136 | 159,952 |
| CASTLE_Q | False | 0 | 4 / 4 | 1,035,956 | 1,322,232 | 286,276 |
| SWAMP_B | False | 0 | 14 / 14 | 924,740 | 1,411,513 | 486,773 |
| ICE_B | False | 0 | 29 / 29 | 1,297,252 | 1,918,743 | 621,491 |
| UNDER_B | False | 0 | 30 / 30 | 1,762,460 | 2,230,797 | 468,337 |

The known pre-existing fact is reconfirmed: every variant walks event-free but
ends short of the `TFA\xff` anchor because further resource tables follow.
**Does this block them as targets? For the question asked, it is irrelevant** —
they are boss-arena resource sets, not normal-play levels, and no cross-world
arrival is wanted there. If they were ever wanted as targets, yes, it blocks
them: `assess_target_table` refuses, so `target_trust()` would fail and no port
would be attempted. The refusal is correct as it stands and should not be
relaxed to reach them.

---

## 4. The sound-ceiling contradiction

### The contradiction, MEASURED

`sound_bank.SPU_USABLE_CEILING` = 1,966,080 (= 0x1E0000).
`HEADROOM_CONSERVATIVE` = 68,672. `MAX_SFX` = 300.

| bank | file | nSFX | header | sample bytes | roundup64(file) | roundup64(samples) | file − ceiling | samples − ceiling |
|---|---|---|---|---|---|---|---|---|
| GRAVE | 1,893,776 | — | — | — | 1,893,824 | — | −72,256 | — |
| SWAMP | 1,952,156 | 286 | 3,436 | 1,948,720 | 1,952,192 | 1,948,736 | −13,888 | −17,344 |
| ICE | 1,962,472 | 283 | 3,400 | 1,959,072 | 1,962,496 | 1,959,104 | −3,584 | −6,976 |
| UNDER | 1,975,052 | 286 | 3,436 | 1,971,616 | 1,975,104 | 1,971,648 | **+9,024** | **+5,568** |
| CASTLE | 1,976,728 | 299 | 3,592 | 1,973,136 | 1,976,768 | 1,973,184 | **+10,688** | **+7,104** |

First candidate explanation eliminated: the header is exactly 12 B/entry plus a
4-B count, so charging only the SPU-resident sample block instead of the whole
file removes 3.4–3.6 KB — **not enough**. UNDER and CASTLE still exceed the
constant by 5,568 and 7,104 B. The ceiling as stated really is falsified by
retail.

### Which of the four candidates it is

MEASURED from `research/MJOLNIR_IRX_SOUND_DRIVER.md` (prior work on the IRX):
`SOUND_EnableEffects` (**IRX command 19**, @0x3EC4) is what places the reverb
work area, at **0x001FFFFF for the first core and 0x001DFFFF for the second**.
The constant 1,966,080 = 0x1E0000 is therefore the region below the FIRST core's
reverb area — reverb reserving the top 128 KB, from the top down. The IRX itself
performs **no ceiling check of any kind**.

MEASURED in `SLUS_200.17` (load vaddr 0x00100000, file base 0x80): every `jal`
to the RPC helper at 0x0010FBF8, with the RPC function number recovered from the
preceding `li $a1, n`:

- **fno = 19 (`SOUND_EnableEffects`) has exactly ONE call site in the entire
  executable: 0x00112D38.** It sits inside the wrapper function starting at
  0x00112C24, and that wrapper has **zero `jal` callers anywhere in the image.**
- fno = 14 (`StreamMemoryEnd`) has FOUR call sites, not the three in the brief:
  0x001120E8, 0x00141714, 0x00141858, 0x00196844. (The brief's
  0x00141704/0x00141848/0x0019682C are the `li $a1,14` instructions immediately
  preceding three of them.) 0x001120E8 likewise sits in a wrapper (starting
  0x00111F04) with **zero `jal` callers**. Of the three live sites,
  0x00141714 and 0x00141858 pass a populated 1,044-byte send buffer
  (`$t0 = 1044`) whose tail from +0x14 is a 1,023-char string NUL-terminated at
  +0x413 — so `args[0]`/`args[1]` are the first two words of a filename-bearing
  request block — while 0x00196844 passes `send = NULL, ssize = 0`.

**Resolution (INFERRED, from MEASURED call-graph evidence): reverb is not
enabled in normal play.** The only path that can send IRX command 19 is a
wrapper nothing calls. If `SOUND_EnableEffects` is never sent, the reverb work
area is never placed, the top 128 KB is never reserved, and the 0x1E0000 bound
does not describe the hardware state the retail banks are loaded into. That is
exactly the state CASTLE and UNDER need in order to be legal, and it is the only
one of the four candidates the evidence supports:

- "SPU base is lower than assumed" — **eliminated as an explanation.**
  `StreamMemoryEnd = (args[0]<<16)|args[1]` is a base/cursor; a base above zero
  makes the fit WORSE, never better. It cannot explain banks exceeding a ceiling.
- "reverb is only reserved on one core" — the constant already corresponds to
  one core's reservation (core 0 at 0x1FFFFF). Core 1's area at 0x1DFFFF would
  make the usable region 0x1C0000 = 1,835,008, under which even GRAVE would be
  illegal. So the constant is not a two-core figure and relaxing to one core
  buys nothing.
- "the constant is simply wrong" — true as a description of retail, but the
  interesting reason is the one above, not arithmetic error.

**Caveat, stated plainly.** The wrapper functions are reached by no `jal`; they
could still be reached through a function pointer, and the writers of the
1,044-byte send buffers at 0x00141714/0x00141858 (which carry `args[0]`/`args[1]`
for command 14) were not traced to their sources here. So the SPU base the EE
actually installs is still unknown. **The real per-world SPU headroom therefore
cannot be tightly bounded without that trace.** What can be stated:

| world | bank roundup64 | headroom under the current (conservative, reverb-on) 1,966,080 | headroom under the reverb-off 2 MB structural limit (2,097,152) |
|---|---|---|---|
| GRAVE | 1,893,824 | 72,256 (project uses 68,672) | 203,328 |
| SWAMP | 1,952,192 | 13,888 | 144,960 |
| ICE | 1,962,496 | 3,584 | 134,656 |
| UNDER | 1,975,104 | **negative (−9,024)** | 122,048 |
| CASTLE | 1,976,768 | **negative (−10,688)** | 120,384 |

The right-hand column is a STRUCTURAL upper bound (2 MB of SPU2 RAM, reverb
off, base 0) and is NOT a safety bound — the unknown `StreamMemoryEnd` base
subtracts from it. `SPU_USABLE_CEILING` and `HEADROOM_CONSERVATIVE` were NOT
changed, as instructed: GRAVE is verified working against them.

**CASTLE additionally cannot take sound imports at any size.** MEASURED:
`CASTLE.BD` nSFX = **299** against the driver's `< 300`. One import would need
one free entry slot and there is exactly zero. Byte headroom is moot.

---

## 5. Verdict per world

- **ICE — VIABLE.** Gate passes (0 resyncs, anchor exact). 2,398,271 B of pool
  headroom, ~4 median-cost arrivals (up to 10 cheap ones). 13 eligible foreign
  routes, the largest set of the four. Sound: possible in principle but the
  byte headroom under the current conservative bound is only 3,584 B, i.e.
  effectively zero, so treat ice as assets-plus-maybe-one-tiny-sound until the
  SPU base is traced. Caution: 4 of its 13 routes (0x2F, 0x37, 0x48, 0x6A —
  grave natives) have never been ported anywhere.
- **SWAMP — VIABLE.** Gate passes. 1,937,684 B headroom, ~3 median arrivals
  (up to 8 cheap). 10 eligible foreign routes, all 10 already measured into
  grave. Sound: 13,888 B under the conservative bound — enough for a small
  import set, not for a boss-sized one.
- **UNDER — ASSETS ONLY.** Gate passes. 1,641,427 B headroom, ~2–3 median
  arrivals. 10 eligible foreign routes, 9 already measured into grave. Sound
  is impossible under the current bound: `UNDER.BD` roundup64 is 1,975,104,
  which is 9,024 B ABOVE `SPU_USABLE_CEILING` before any import — the shipped
  bank already fails the bound, so no import can be admitted without first
  resolving §4 and moving a safety constant, which this pass deliberately does
  not do.
- **CASTLE — MARGINAL, ASSETS ONLY.** Gate passes, but 802,663 B is the
  smallest headroom of any world: **1 median-cost arrival, 2 only if both are
  its cheapest routes** (0x0D + 0x19 = 790,079 B, 12,584 B to spare). Sound is
  permanently impossible: nSFX 299 against `< 300`, independent of bytes. Only
  8 eligible foreign routes. Measuring 8 route costs to place 1–2 enemies is
  the worst return of the four.

### Scope recommendation

**Build for a SUBSET: ICE and SWAMP, assets only.** They carry 88% of the
affordable arrival capacity outside grave (2,398,271 + 1,937,684 of the
6,779,000 B total non-grave headroom is 64%, and they afford ~7 of the ~10
median arrivals), their gates are clean, and 19 of their 23 eligible routes
involve an enemy that has already ported cleanly into grave once. That is 23
route costs to measure.

**Add UNDER only if ~2–3 more arrivals justify 10 more measurements**, and only
as assets — its shipped bank already sits above the sound bound.

**Do not build CASTLE.** One-to-two arrivals for eight route measurements, with
sound structurally impossible at nSFX 299. If the multi-world feature ships,
castle should be an explicit refusal with that reason recorded, not an
unmeasured gap.

**Do not touch the sound path for any new world** until the `StreamMemoryEnd`
writers at 0x00141714/0x00141858 are traced. Assets-only multi-world is
buildable today on the numbers above; multi-world sound is not.
