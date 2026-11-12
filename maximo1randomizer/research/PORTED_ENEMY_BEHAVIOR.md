# Ported Enemy Behavior Repair — Research Document

Spec: `.kiro/specs/ported-enemy-behavior-repair/`

This document is created by Task 1 (Band 0, prerequisite gate). It will be
extended by Task 24. At present it contains only the prerequisite verdict.

## Prerequisite status

**Verdict: BLOCKED on `bef-script-vm-disassembler`.**

Measured facts, from a probe run on the working copy (background process, fresh
terminal, no files written, no binary parsed):

| check | result |
|---|---|
| `randomizer/befscript.py` exists | **no** |
| any `randomizer/*script*` module exists | **no** (empty glob) |
| `Opcode_Map` non-empty | **not evaluable** — module absent |
| `disassemble` importable | **not evaluable** — module absent |
| `Instruction` importable | **not evaluable** — module absent |
| `Script_API_Table` index map importable | **not evaluable** — module absent |
| `.kiro/specs/bef-script-vm-disassembler/` exists | yes |
| its files | `requirements.md`, `design.md` only |
| its `tasks.md` exists | **no** |
| any implementation of that spec | **none found** |

The blocker is therefore `bef-script-vm-disassembler`: it has requirements and a
design but no task plan and no code, so the `Opcode_Map`, `Script_Disassembler`,
`Instruction` type and `Script_API_Table` index map that this feature consumes as
**inputs** (Req 1.1) do not exist.

### Consequences applied

- Per **Req 1.2**, no change was made to the porting behaviour of any module
  under `randomizer/`. Task 1 wrote no code at all: for any fixed seed the
  emitted `.PRS`, `.PRT` and `.BEF` buffers and the set of accepted routes are
  identical to a build without this feature. Read-only, decode-free additions
  that no porting path reaches remain permitted for Band 1 (Task 2's
  `script_section_bounds`).
- Per **Req 1.1**, no opcode decoding was implemented here. Opcode decoding is
  entirely within `bef-script-vm-disassembler`'s scope.
- No stub, placeholder or partial `Opcode_Map` was created anywhere — not in
  `randomizer/`, not in `tests/`, not as a fixture. A synthetic map remains
  permitted **only** as a test-local fixture for the pure-logic tests of Tasks
  3–6, and must never be importable as a production default.
- Per **Req 3.10**, `SCRIPT_OPERAND_REMAP_ENABLED` must default to `False` when
  it is introduced in Task 7, and per **Req 1.2a** an unavailable prerequisite
  must be a reported no-op pass-through rather than a route refusal.
- Per **Req 1.3**, no decode was performed, so no Encoding_Revision selection
  arose.

### Verification_Case state while blocked (Req 1.2b)

While the `Opcode_Map` is unavailable, every Verification_Case's `Remap_Class`,
every Static_Check result and every Route_Status is **`UNDETERMINED`**. No
Research_Document verdict is emitted for any case.

| Verification_Case | route | reported symptom | Remap_Class | Static_Check | Route_Status |
|---|---|---|---|---|---|
| `0x53` Dark_Knight | CASTLE → GRAVE | inert until hit | UNDETERMINED | UNDETERMINED | UNDETERMINED |
| `0x3A` PirateSkeleton | ICE → GRAVE | attack ends instantly | UNDETERMINED | UNDETERMINED | UNDETERMINED |
| `0x0D` Zombie_Crocodile | SWAMP → GRAVE | no damage reaction | UNDETERMINED | UNDETERMINED | UNDETERMINED |
| `0x1D` Snowman | ICE → GRAVE | wrong shockwave cadence | UNDETERMINED | UNDETERMINED | UNDETERMINED |
| `0x49` Bomb_Skeleton | CASTLE → GRAVE | explodes when hit | UNDETERMINED | UNDETERMINED | UNDETERMINED |

The Script_Section hypothesis stated in the spec's Introduction is likewise
**UNDETERMINED**: it cannot be confirmed or refuted without decoded bytecode.
The one hypothesis-relevant fact obtainable while blocked is Task 2's decode-free
`section_identical` byte comparison, which has not yet been run.

### What is not determined

- Every operand classification, for every Verification_Case.
- Whether any Script_Operand is in fact an animation index, state id, spawn type
  id or timer.
- Decode_Coverage and Clean_Decode for any Script_Section.
- Size_Delta (nothing was built; measurement belongs to Task 21).
- Whether the current pipeline leaves the Script_Section byte-identical
  (Task 2, decode-free, still to run).

### To unblock

Complete `.kiro/specs/bef-script-vm-disassembler/`: give it a `tasks.md`,
implement `randomizer/befscript.py` with a non-empty `Opcode_Map`, `disassemble`,
`Instruction` and the `Script_API_Table` index map, then re-run this gate. Band 2
(Tasks 9–18) cannot start until it reports available. Band 1 (Tasks 2–8) is
executable now against decode-free comparisons and test-local synthetic maps.

## Task 2 — Does the current pipeline leave the Script_Section byte-identical? (Req 2.6)

**Verdict: YES, for all five Verification_Cases, and non-vacuously.** The founding
premise of this spec survives its cheapest available test.

This check is **decode-free**. No `Opcode_Map` was consulted, nothing was
disassembled, and no operand was classified — so nothing here moves any
Verification_Case off `UNDETERMINED` (Req 1.2b still holds in full). The only
claim made is the byte-level one Req 2.6 asks for.

### Method (measured, not inferred)

For each case, in its **own** background process in a **fresh** terminal (Req 9.7
— the four-binary limit; source `.BEF`, source `.PRS`, target `.BEF`, target
`.PRS` per process, plus the target `.PRT` the session unavoidably reads):

1. `BefFile.parse(<SOURCE>.BEF)`, take `get_entry(type_id).data` — the source
   BEF_Blob.
2. `script_port.script_section_bounds(blob)` → `(u32@0x38, u32@0x0C)`; the source
   Script_Section is `blob[start:end]`.
3. Build a fresh `WorldPortSession("grave", game_files)` and drive the route
   through the **unmodified** gated path: `session.port_enemy(PlacementPlan(...,
   path=cross_world.PORT, source_world=<src>), blob)`. No gate was relaxed and no
   allowlist filter was bypassed — the plan is constructed directly, exactly as
   the existing `tests/test_cross_world_allowlist_expansion.py` and
   `tests/test_bisect_step3_snowman_isolation.py` drive single routes, which is
   also how `0x3A` PirateSkeleton (`active=False`, in scope per Req 4.6) was
   evaluated.
4. Take the ported blob's own bounds and compare the two sections byte-for-byte.

Everything ran in memory. No file was written; `game_files/` was opened
read-only only (Req 9.5).

### Results

| case | route | port | source bounds `(0x38, 0x0C)` | ported bounds | Script_Section length | `section_identical` | substance |
|---|---|---|---|---|---|---|---|
| `0x53` Dark_Knight | CASTLE → GRAVE | ok, not refused, not fallback | `(764, 3266)` | `(764, 3266)` | **2,502 B** | **True** | non-vacuous |
| `0x3A` PirateSkeleton | ICE → GRAVE | ok, not refused, not fallback | `(1188, 5207)` | `(1188, 5207)` | **4,019 B** | **True** | non-vacuous |
| `0x0D` Zombie_Crocodile | SWAMP → GRAVE | ok, not refused, not fallback | `(488, 2579)` | `(488, 2579)` | **2,091 B** | **True** | non-vacuous |
| `0x1D` Snowman | ICE → GRAVE | ok, not refused, not fallback | `(713, 3024)` | `(713, 3024)` | **2,311 B** | **True** | non-vacuous |
| `0x49` Bomb_Skeleton | CASTLE → GRAVE | ok, not refused, not fallback | `(795, 4831)` | `(795, 4831)` | **4,036 B** | **True** | non-vacuous |

SHA-256 prefixes of the source and ported sections (identical in every case, and
identical to each other by construction of the comparison):
`0x53` `c4e0d3c44c4c6e13`, `0x3A` `63f69ffd534fb731`,
`0x0D` `cde37d28260b50b1`, `0x1D` `ca1228c9acb14e04`,
`0x49` `a136f084fcd7feca`.

Blob total length was unchanged by the port in every case (3,312 / 5,248 / 2,624 /
3,056 / 4,864 B in and out), and the Script_Section start and end words were
unchanged, so the mesh-ref remap and the anim stage demonstrably confine
themselves to the earlier sections.

### The vacuity hazard, addressed explicitly

Byte-identity over a zero-length section would be trivially true and would prove
nothing — the same hazard Static_Check 7 (Req 5.8) exists to catch. **No case is
vacuous.** The shortest Script_Section measured is 2,091 B (`0x0D`) and the
longest 4,036 B (`0x49`); every case compared thousands of bytes of real
bytecode. Recorded so a reader can see the comparison had substance rather than
taking "identical" on trust.

### What this establishes, and what it does not

**Measured fact.** The current Port_Pipeline copies the Script_Section verbatim
for all five Verification_Cases. The design's hook point — a new stage inside
`_port_enemy`, after the anim branch and before the ref proof — is therefore
sound, and its one-map-two-consumers commitment is not contradicted by any
existing script write, because there is no existing script write. Bands 1–2 are
worth continuing.

**Inference, not fact.** That the verbatim copy is the *cause* of the five
reported symptoms remains a **hypothesis**. Byte-identity proves the operands are
still source-world numbers; it does **not** prove any particular operand is an
animation index, nor that a stale index is what makes the Dark_Knight inert. That
requires the decoded bytecode, which requires
`bef-script-vm-disassembler`. Hypothesis status stays **UNDETERMINED**.

### Code added by this task

`randomizer/script_port.py`, containing **only** `script_section_bounds` — a
read-only, decode-free reader of `(u32@0x38, u32@0x0C)` that returns
`(None, None)` rather than raising when the span is unusable (Refusal Model
condition 4: blob too short for both words, `u32@0x38 > u32@0x0C`, or either
bound past the blob end). An empty span (`start == end`) is deliberately **not** a
refusal — it is a real empty section, and reporting it as such is what lets a
caller mark a comparison VACUOUS instead of passing.

No opcode map, no stub map, no classification, no remap — those are Tasks 3–5.

**Req 1.2 assertion, machine-checked:** a scan of every `randomizer/*.py` for the
token `script_port` returned **no module other than `script_port.py` itself**.
Nothing in the porting path imports it, so it is reached from no porting path
until Task 7, and the emitted `.PRS`/`.PRT`/`.BEF` buffers and accepted route set
for any seed are unchanged.

### Deferred from optional sub-task 2.1

`tests/test_cross_world_script_switch.py` was **not** written. Its subject,
`SCRIPT_OPERAND_REMAP_ENABLED`, does not exist until Task 7, so the Property 13
byte-identity-with-the-switch-off test has no switch to disable and would be
vacuous today. It is deferred to Task 7.1, which already specifies it. The
Req 1.2 guarantee it would pin is instead evidenced here by the import-graph scan
above and by the unchanged blob lengths and bounds in the results table.

### Data safety for this task (Req 9.1, 9.2, 9.5, 9.6)

- Every analysis process read `game_files/` only; no write mode was ever opened.
- Post-run inventory of `game_files/` (recursive): 101 files, **158,393,751 bytes** total,
  aggregate SHA-256 over `(name, sha256(content))` pairs
  `f0692397c9bbc2105486b932b20af011d363c7419bd78db95fa6e3c2e2c2286d`. That walk
  hashes without parsing, so it is exempt from the four-binary limit. This figure
  is the baseline for Task 19's before/after comparison.
- One target `.PRS` resident per process, plus the transient buffers a port
  unavoidably reads and drops.
- No temporary, scratch or intermediate file was created anywhere, including
  `%TEMP%`. No `.PRS`/`.PRT`/`.BEF`/`.PSX`/ELF and no disc image was written.
- Re-measured after the Baseline_Suite run: same 101 files, same 158,393,751
  bytes, same aggregate SHA-256 `f0692397…2286d`. `game_files/` is unchanged.
- Baseline_Suite after this task: **316 passed / 0 failed** (`python -m pytest -q
  --no-header -p no:cacheprovider tests` from `maximo1/`, background process,
  fresh terminal). No regression; no test was added by this task.
---

## Task 1 RE-GATE — 2026-08-09 — prerequisite is now AVAILABLE

**This supersedes the `BLOCKED` verdict recorded above. The earlier verdict is
kept deliberately: it was correct on the day it was written.**

`.kiro/specs/bef-script-vm-disassembler/` has since been implemented. Probe run
as a background process in a fresh terminal, no file written, no binary parsed:

| check | result |
|---|---|
| `randomizer/befscript.py` exists | **yes** |
| `opcode_map(REVISION_RETAIL)` size | **158** opcodes |
| `opcode_map(REVISION_PROTOTYPE)` size | **158** opcodes |
| Confidence_Levels present in the map | `['verified']` only — all 158 |
| `disassemble` importable and callable | yes |
| `Instruction` fields | `(offset, opcode, raw, mnemonic, operands, annotation)` |
| `Operand` fields | `(name, kind, value)` |
| Script_API_Table index map | `API_SLOTS` = **254**, `API_BY_INDEX` = 254, `api_slot()` total over 0-253 |
| `INVALID_OPCODES` | 98 slots, `0x00` among them |
| `BY_PAIRS` narrow→wide | 44 pairs |
| `API_ARITY_UNCERTAIN` | `{62, 170}` |
| `API_INDIRECT_SLOTS` | `{58, 68, 69, 135, 171, 192, 193, 217, 224, 227}` |
| Encoding_Revision always explicit (Req 1.3) | **yes** — `disassemble(section, None)` raises `ValueError: unknown encoding revision None` rather than inferring |
| its spec dir now holds | `requirements.md`, `design.md`, `tasks.md`, `.config.kiro` |

**Gate verdict: AVAILABLE.** Req 1.2's blocked path no longer applies, and
**Req 1.2b's `UNDETERMINED` hold is lifted** — Verification_Case verdicts may now
be emitted, and the Stage A verdicts below are the first of them.

Every decode reported in this document passed `REVISION_RETAIL` explicitly. All
five targets are retail BEF files.

---

## Tasks 10–14 — Stage A diagnosis of the five Verification_Cases

Method, per case: **one enemy per background process in a fresh terminal**, at
most four binaries parsed per process (source `.BEF`, source `.PRS`,
`GRAVE.PRS`, `GRAVE.BEF`), only one `.PRS` resident at a time, everything in
memory, nothing written anywhere including `%TEMP%`.

### The calling convention, measured (this corrected a wrong first attempt)

The first tracer looked backwards from each `callapi` for `*_apiparam` pushes
(`0x8D`–`0x90`) and found **zero arguments on every call**. That was the tracer
being wrong, not the scripts being empty. The compiler these scripts came out of
does **not** use the `*_apiparam` opcodes. It uses the operand stack:

```
by_c_push value=0x6          <- pushed first
by_c_push value=0x2
by_c_push value=0x1          <- pushed last
by_popsetapiparam count=0x3  <- moves 3 stack values into the API arg block
by_callapi api_index=0x29    <- CScriptApi::SeqPlay, arity 3
```

`popapiparam` (`0x15`) moves one value; `popsetapiparam` / `by_popsetapiparam`
(`0x16`/`0x17`) move `count`. So the argument trace is a small stack machine over
the `*_push` family, drained by the `pop*apiparam` family, consumed by
`callapi`/`by_callapi`. Corroborating single case, seen dozens of times:
`c_push -1 ; popapiparam ; by_callapi 0x0E` = `GetClassID(-1)`, `-1` being the
engine's conventional self handle.

**Measured:** which push supplies which arg block slot, and every literal value.
**Inference:** the order in which `popsetapiparam` fills the block. This document
reports `arg0` = first popped = **last** pushed. Whether the engine's argument 0
is that one or the first-pushed one is **not determined** and does not affect any
conclusion below, because the conclusions turn on *which literal varies*, not on
its index.

### Decode quality — all five, measured

| case | route | section span `(0x38, 0x0C)` | len | instructions | Decode_Coverage | Clean_Decode | byte-exact round trip |
|---|---|---|---|---|---|---|---|
| `0x53` Dark_Knight | CASTLE→GRAVE | (764, 3266) | 2,502 | 1,083 | **100.0000%** | **yes** | **yes** |
| `0x3A` PirateSkeleton | ICE→GRAVE | (1188, 5207) | 4,019 | 1,766 | **100.0000%** | **yes** | **yes** |
| `0x0D` Zombie_Crocodile | SWAMP→GRAVE | (488, 2579) | 2,091 | 872 | **100.0000%** | **yes** | **yes** |
| `0x1D` Snowman | ICE→GRAVE | (713, 3024) | 2,311 | 1,011 | **100.0000%** | **yes** | **yes** |
| `0x49` Bomb_Skeleton | CASTLE→GRAVE | (795, 4831) | 4,036 | 1,672 | **100.0000%** | **yes** | **yes** |

Zero decode failures across all five. `section_identical` remains **True** for
all five from Task 2 above; nothing in this run contradicts it.

### THE CENTRAL MEASUREMENT — `SeqPlay`'s index is entity-local, not a world index

This is the finding that decides the spec's founding hypothesis, so the evidence
is given in full.

`41 SeqPlay` (arity 3) is called with three literals in every single occurrence
across all five enemies. Two of the three barely move; one sweeps a dense range
from 0:

| case | `SeqPlay` calls | arg0 values | arg1 values | **varying arg** |
|---|---|---|---|---|
| `0x53` Dark_Knight | 12 | `{1}` | `{0, 2}` | **0–12**, dense |
| `0x3A` PirateSkeleton | 22 | `{0, 1}` | `{0, 2}` | **0–16**, dense |
| `0x0D` Zombie_Crocodile | 8 | `{1}` | `{0, 2}` | **0–5**, dense |
| `0x1D` Snowman | 11 | `{0, 1}` | `{0, 2}` | **0–9**, dense |
| `0x49` Bomb_Skeleton | 14 | `{1}` | `{0, 2}` | **0–11**, dense |

That alone is suggestive. The decisive test is a corpus one. Disassembling
**every** type in `GRAVE.BEF` (51 types carry `SeqPlay` literals) gives:

```
GRAVE 0x01 [0]            GRAVE 0x02 [0,1,2]        GRAVE 0x07 [0,1,2]
GRAVE 0x0A [0,1,2,3]      GRAVE 0x0D [0,1,2,3,4,5]  GRAVE 0x0E [0,1]
GRAVE 0x16 [0,1]          GRAVE 0x24 [0,1,2,3]      GRAVE 0x25 [0..12]
GRAVE 0x2F [0..14]        GRAVE 0x37 [0..9]         GRAVE 0x39 [0..15]
GRAVE 0x47 [0..18]        GRAVE 0x48 [0..11]        ... 51 types total
```

- Fifty-one distinct entity types in **one** world overwhelmingly reuse the same
  low indices; a dozen of them play index `0`, `1` and `2`.
- The largest index anywhere in `GRAVE.BEF` is **18**, while `GRAVE.PRS` holds
  **53** anim records — the top two thirds of the world's anim table is named by
  no script at all.
- `GRAVE`'s own native type `0x0D` uses exactly `[0,1,2,3,4,5]`, **the same set**
  as `SWAMP`'s Zombie_Crocodile `0x0D`.

A world-global animation index cannot behave like that. Fifty-one entities are
not all animated by record 0. **`SeqPlay`'s index is resolved per entity**,
through the entity's own sequence table inside its BEF_Blob — which the port
copies along with the blob, and whose CATEGORY-2 animation references the
existing anim stage already repoints.

### Animation-index presence, source vs target (Req 2.3)

Recorded because Req 2.3 asks for it, and reported as **not meaningful** for the
reason just given — these values are not indices into these tables.

| case | source `.PRS` records | `GRAVE.PRS` records | literal range | resolves in source | resolves in GRAVE |
|---|---|---|---|---|---|
| `0x53` | CASTLE 109 (clean) | 53 (clean) | 0–12 | all | **all** |
| `0x3A` | ICE 82 (clean) | 53 | 0–16 | all | **all** |
| `0x0D` | SWAMP 78 (clean) | 53 | 0–5 | all | **all** |
| `0x1D` | ICE 82 | 53 | 0–9 | all | **all** |
| `0x49` | CASTLE 109 | 53 | 0–11 | all | **all** |

Not one animation literal in any of the five sections fails to resolve in
`GRAVE`. The design named this exact pattern as the one that **forces
`REFUTED`**: "every animation index the section names resolves in both tables".

### What IS world-dependent: spawn type ids (measured)

`49 Spawn` (arity 1) takes a BEF type id. Unlike a sequence index, a type id
must exist in the target world's `BEF_File` — `GRAVE.BEF` carries 70 types.

| case | call site (offset in section) | literal | `0xNN` | present in `GRAVE.BEF` |
|---|---|---|---|---|
| `0x53` Dark_Knight | `Spawn` @234, arg pushed @231 | 140 | `0x8C` | **NO** |
| `0x3A` PirateSkeleton | `Spawn` @354, arg @351 | 53 | `0x35` | yes |
| `0x0D` Zombie_Crocodile | `SpawnAbility` @1062 only | — | — | n/a |
| `0x1D` Snowman | `Spawn` @640, arg @637 | 30 | `0x1E` | **NO** |
| `0x1D` Snowman | `Spawn` @990, arg @987 | 155 | `0x9B` | **NO** |
| `0x49` Bomb_Skeleton | `Spawn` @1288, arg @1285 | 94 | `0x5E` | **NO** |
| `0x49` Bomb_Skeleton | `Spawn` @1965, arg @1962 | 235 | `0xEB` | **NO** |
| `0x49` Bomb_Skeleton | `Spawn` @2509, arg @2506 | 235 | `0xEB` | **NO** |
| `0x49` Bomb_Skeleton | `Spawn` @2664, arg @2661 | 99 | `0x63` | **NO** |
| `0x49` Bomb_Skeleton | `Spawn` @2767, arg @2764 | 68 | `0x44` | **NO** |

`176 SpawnAbility` (arity 3) literals: `0x53` `(0,0,m[4])` @1194 and `(0,0,7)`
@1332; `0x3A` `(12, m[20], m[4])` @1797; `0x0D` `(0,0,7)` @1062; `0x1D`
`(16, m[7], m[3])` @1245; `0x49` `(6, m[17], m[6])` @1781. **Which of its three
arguments is a type id is not determined**, so no presence verdict is claimed for
`SpawnAbility`. Note `0x1D`'s `16` and `0x49`'s `6` are absent from `GRAVE.BEF`
as type ids, `0x3A`'s `12` is present.

Per the per-class rules a `SPAWN_TYPE_ID` is **world-independent as a value** —
it is not remapped, it is *checked*. So this finding is not something an
Operand_Remap can fix. Four of the five enemies call `Spawn` on a type id that
does not exist in `GRAVE.BEF`.

### Timers and state ids (measured, enumerated per Req 2.2)

`9 CreateTimer` / `29 KillTimer` literals, with offsets:

| case | timer operands |
|---|---|
| `0x53` | `CreateTimer(1500)` lit@801 (call@807); `CreateTimer(1500)` lit@1010 (call@1016); `KillTimer(m[16])` @871 |
| `0x3A` | `CreateTimer(2000)` lit@721; `CreateTimer(6000)` lit@1423; `CreateTimer(6000)` lit@1604 |
| `0x0D` | `CreateTimer(1000)` lit@430; `CreateTimer(750)` lit@827 |
| `0x1D` | `CreateTimer(800)` lit@102; `CreateTimer(2500)` lit@526; `CreateTimer(2500)` lit@1045; `CreateTimer(3000)` lit@1436; `KillTimer(m[11])` @1423 |
| `0x49` | **`CreateTimer(m[12])` @1052 (call@1055) — a MEMBER, not a literal**; `KillTimer(m[13])` @2740 |

`46 SetState` literals are all small dense state ordinals (`0x53`: 18 calls over
states 1–11 at offsets 35…2328; `0x3A`: 32 over 1–12; `0x0D`: 15 over 1–9;
`0x1D`: 18 over 0–11; `0x49`: 24 over 0–12). Every `SetState` argument is a
literal `by_c_push` immediately before the call. Script-local, no cross-world
meaning, nothing to remap — enumerated because Req 2.2 asks for it.

### Blob sub-section layout (measured, context for the mechanisms named below)

The Script_Section is the **last** of the blob's eight sections in all five
cases, and the tables before it are what actually resolve the script's small
ordinals:

| case | `0x1C` | `0x20` mat | `0x24` | `0x28` | `0x2C` | `0x30` | `0x34` | `0x38` script |
|---|---|---|---|---|---|---|---|---|
| `0x53` | 60 | 156 | 600 | 612 | 664 | 716 | 752 | 764 |
| `0x49` | 60 | 172 | 615 | 627 | 683 | 739 | 775 | 795 |
| `0x3A` | 60 | 192 | 1004 | 1016 | 1072 | 1128 | 1164 | 1188 |
| `0x1D` | 60 | 140 | 557 | 569 | 621 | 673 | 709 | 713 |
| `0x0D` | 60 | 132 | 340 | 352 | 396 | 440 | 476 | 488 |

- `[0x1C:0x20]` is a fixed-width scalar block — `0x53` reads
  `[19, 6, 84, 0, 30.0f, 15.0f, 40.0f, 0…]`, `0x49` reads
  `[23, 7, 100, 0, 30.0f, 25.0f, 35.0f, 0…, 1, 0]`. **Inference:** the member /
  property defaults, i.e. the source of `0x49`'s `m[12]` fuse duration.
- `[0x28:0x2C]` and `[0x2C:0x30]` are two parallel count-prefixed tables of
  equal length whose values are all **byte offsets inside the Script_Section**
  (`0x53`: 12 entries each, 418…1450 against a 2,502-byte section).
  **Inference:** per-state entry points, one table per phase.
- `[0x30:0x34]` (8 entries) and `[0x34:0x38]` (2 / 4 entries) are further
  offset tables into the script. **Inference:** event handlers.
- `[0x24:0x28]` is `[script_length, 0, 2]` — a self-description.

All of this is **inside the blob and therefore copied verbatim by the port**, so
none of it is stale by itself. What is *not* inside the blob is the world's anim
table and the world's type-id set.

---

## Task 1 / Req 2.4 / Req 2.5 — verdict per Verification_Case

Each case was diagnosed in its own isolated process. No case's result depended on
any other's (Req 2.5, Property 16).

### Hypothesis verdict, overall: **REFUTED** (Confidence_Level: `verified`)

The spec's Introduction predicted "an instruction meaning *play animation 47 and
wait for it to finish* indexes a different or absent record in GRAVE". Measured:
no Script_Section in any of the five names an animation index at all. It names
**entity-local sequence ordinals** (0–16), every one of which resolves in both
worlds, and which fifty-one native GRAVE types reuse among themselves. The
`Operand_Remap` this spec was designed around **has nothing to rewrite in any of
the five sections**: zero `ANIM_INDEX` operands would be remapped, and per the
design's own rules `STATE_ID`, `TIMER` and `SPAWN_TYPE_ID` are all left
unchanged by construction.

Requirement 2.5 is therefore in force, and the mechanisms the evidence *does*
support are named per case below. **No fix was applied to make the story fit.**

### `0x53` Dark_Knight — inert until hit

- **hypothesis: REFUTED** for stale operands. Confidence `verified`.
- Measured: all 12 `SeqPlay` ordinals 0–12 resolve; both `CreateTimer` literals
  are 1500 ms and unchanged by the port; the aggro branch exists and is intact —
  `GetDistance(-1)` @2258 (`c_push -1` @2252), result compared
  `by_frr_ifcmpgte` against `c_push 0x41000000` = **8.0f** @2262, and on the
  taken side `SetState(4)` @2289. Three `GetDistanceSquared` calls back it up.
  Nothing in that branch is world-dependent.
- **Mechanism the evidence supports** (Confidence `inferred`): the entry block
  runs `SetState(1)` @38 → `SeqPlay(seq 0)` @229 → **`Spawn(0x8C)` @234**, and
  `0x8C` is **absent from `GRAVE.BEF`**. An init path whose second action is a
  spawn of a nonexistent type is the most plausible reason the entity never
  leaves its entry state until an external event (a hit) re-enters the state
  machine. This is a **companion-entity gap**, owned by
  `.kiro/specs/companion-entity-porting/` (verdict `UNRESOLVED`) — the purple
  portal. **Not restored here** (Req 8.1). Req 4.1 concerns the aggro state
  only, not the portal (Req 8.5).
- Competing candidate not excluded: the level script in `C_*.PSX` / `GRAVE`'s
  `.PSX`, which is where per-instance placement properties live. Not decoded
  (out of scope).

### `0x3A` PirateSkeleton — attack ends instantly

- **hypothesis: REFUTED**, and the specific predicted mechanism is **refuted by
  name**. Confidence `verified` for the refutation.
- The design predicted: `41 SeqPlay` followed by a `27 IsSequenceComplete` wait
  loop where the played sequence does not resolve in GRAVE, so the wait returns
  at once. Measured: the pattern **is** there — `SeqPlay(…,0,0)` @816 then
  `IsSequenceComplete` @901; `SeqPlay(…,0,0)` @929; `SeqPlay(…,0,4)` @711 then
  `IsSequenceComplete` @790 — but **every** sequence ordinal it plays (0–16)
  resolves, and `27 IsSequenceComplete` has **arity 0**: it takes no argument at
  all, so there is no operand on it to be stale. Both of its `Spawn`-side
  literals (`0x35` type, `SpawnAbility` 12) are present in `GRAVE.BEF`. This
  case has **no** absent-in-target operand of any class.
- **Mechanism the evidence supports** (Confidence `unknown` — this is a
  candidate list, not a conclusion): the failure is downstream of the script.
  Ranked candidates: (1) the blob's own **sequence table**, whose CATEGORY-2
  animation references the anim stage repoints or substitutes — if the record
  behind ordinal 0/4 was substituted rather than ported, `SeqPlay` can start
  nothing and an arity-0 completion query answers immediately; this route is
  `active=False` and per Req 4.6 may never have run the port arm at all;
  (2) rig binding of the attack clip against the target rig; (3) the level
  `.PSX`. **Not** the Script_Section operands.
- This case is the clearest demonstration that the Operand_Remap would not have
  fixed it, and would have reported success while fixing nothing.

### `0x0D` Zombie_Crocodile — no damage reaction

- **hypothesis: REFUTED** for stale operands. Confidence `verified`.
- Measured: 8 `SeqPlay` calls, ordinals 0–5, all resolve in both worlds. The hit
  path is intact and readable: `SetState(6)` @891 → `SeqPlay(1, 2, 3)` @918 →
  `SetState(2)` @946, with `IsSequenceComplete` waits at @732, @1033, @1173 and
  `TriggerAnimFrame` float cues at @754, @788, @1162. Timer literals 1000 ms
  @430 and 750 ms @827 are world-independent. Its only spawn-family call is
  `SpawnAbility(0, 0, 7)` @1062.
- **Mechanism the evidence supports** (Confidence `unknown`): same candidate set
  as `0x3A` — the reaction clip behind ordinal 3 either was not ported (the anim
  stage's `new_indices` did not cover it) or does not bind to the target rig.
  Both are checkable by Static_Checks 1 and 2 against the blob's sequence table,
  **not** by rewriting a script operand.
- Classification note for Req 7: an absent hit-reaction animation with damage and
  death still working is the design's own example of a **Cosmetic_Gap**. The
  classification is deferred to Task 17 and is **not** made here.

### `0x1D` Snowman — shockwave on the wrong cadence

- **hypothesis: REFUTED** for stale operands. Confidence `verified`.
- Measured, timer operands enumerated with offsets as Task 13 requires:
  `CreateTimer(800)` lit@102, `CreateTimer(2500)` lit@526, `CreateTimer(2500)`
  lit@1045, `CreateTimer(3000)` lit@1436, `KillTimer(m[11])` @1423. These are
  **durations in milliseconds, not indices** — they mean the same number in
  every world and the port copies them unchanged, so the cadence the script
  specifies in GRAVE is byte-for-byte the cadence it specifies in ICE. A stale
  operand cannot change a cadence that was never world-relative.
- **Mechanism the evidence supports** (Confidence `inferred`): the shockwave's
  *effect*, not its clock. `Spawn(30 / 0x1E)` @640 and `Spawn(155 / 0x9B)` @990
  are **both absent from `GRAVE.BEF`**, and `SpawnAbility(16, …)` @1245 names a
  `16` that is also absent as a type id. A shockwave that is a spawned entity
  cannot appear at all in GRAVE while the timer keeps cycling on schedule —
  which is exactly what "wrong cadence" looks like from the player's side. The
  reported symptom may itself be a misattribution of a missing-entity gap.
- What would settle it: whether `0x1E` / `0x9B` are the shockwave apparition.
  **Not determined** — that is companion-entity territory.

### `0x49` Bomb_Skeleton — explodes the moment it is hit

- **hypothesis: REFUTED** for stale operands. Confidence `verified`.
- Measured, and this one is decisive on its own: the fuse is **not a literal**.
  `CreateTimer` @1055 takes `by_m_push member=12` @1052 — the duration comes
  from **member field 12**, and `KillTimer` @2740 takes `m[13]`. There is **no
  timer literal anywhere in this section**. An Operand_Remap over script literals
  cannot touch the fuse, because the fuse is not in the bytecode.
- Also measured: all 14 `SeqPlay` ordinals 0–11 resolve; the hit path is
  `SeqPlay(1,0,2)` @1050 → `IsSequenceComplete` @1082 → `GetAnimFrameCount`
  @1119 → `TriggerAnimFrame` @1232, with `CreateTimer(m[12])` @1055 inside it;
  `211 CreateExplosion` does **not** appear in this section's call trace (it
  appears in `0x53`'s), so the detonation is delegated to a spawned entity.
- **Mechanism the evidence supports** (Confidence `inferred`): **five** `Spawn`
  literals, `0x5E` @1285, `0xEB` @1962, `0xEB` @2506, `0x63` @2661 and `0x44`
  @2764, and **none of the five exists in `GRAVE.BEF`**. Plus the member-field
  initialiser question: `m[12]`'s default lives in the blob's `[0x1C:0x20]`
  scalar block (copied verbatim, so intact) *unless* the level `.PSX` overrides
  it per placement — in which case a ported instance carries no override and the
  fuse could read 0, which is exactly "explodes the moment it is hit". Both
  candidates sit outside the Script_Section.
- Per Task 14 and Req 8.4: the bomb's `Spawn` type id is recorded here as a
  **known companion-entity gap**, the route is not failed for it, and **no
  attempt was made to restore the bomb**. `COMPANION_PORT_ENABLED` is untouched
  at `False`.

### Verdict summary

| case | hypothesis | Confidence | mechanism the evidence supports | class of that mechanism |
|---|---|---|---|---|
| `0x53` Dark_Knight | **REFUTED** | verified | `Spawn(0x8C)` in the entry block, type absent from `GRAVE.BEF` | companion entity (out of scope) |
| `0x3A` PirateSkeleton | **REFUTED** | verified | blob sequence table / anim substitution / rig binding — **not** the script | anim porting, needs Static_Checks 1–2 |
| `0x0D` Zombie_Crocodile | **REFUTED** | verified | reaction clip not ported or not rig-bound | anim porting |
| `0x1D` Snowman | **REFUTED** | verified | `Spawn(0x1E)` and `Spawn(0x9B)` absent from `GRAVE.BEF` | companion entity (out of scope) |
| `0x49` Bomb_Skeleton | **REFUTED** | verified | fuse is `m[12]`, not a literal; five absent `Spawn` type ids | member init + companion entity |

Route_Status for all five stays **`UNDETERMINED`**, not `CANDIDATE`: the
non-vacuity rule (Req 5.8, Property 20) applies directly — the classified
`ANIM_INDEX` operand set is **empty** for every case, so every Static_Check that
inspects it would pass vacuously. A green suite here would mean nothing, and is
recorded as `UNDETERMINED` instead. Nothing was promoted to `CONFIRMED`; only the
user's in-game observation can do that (Req 6.2, 6.3).

### Consequence for the repair — flagged, not acted on

Tasks 3–7 and 15 were **not** started, and this run made **no** code change.
Diagnosis only, as scoped. The finding that the repair plan must now absorb:

> An Operand_Remap of the Script_Section would rewrite **zero operands** in all
> five Verification_Cases, and would then be reported as having succeeded.

Req 2.5's obligation is therefore live: **the repair requirements need revision
for all five cases.** That decision belongs to the user, not to this run. The
generic pipeline stage may still be worth building for its Static_Checks —
checking `SPAWN_TYPE_ID` presence against the target `BEF_File` (Req 5.5) is the
one check in the design that fires on real evidence here, on 4 of 5 cases — but
its *remapping* half has no demonstrated work to do.

### What was NOT determined

- Which of `SeqPlay`'s three arguments the engine calls argument 0, and hence
  whether the varying ordinal is argument 0 or argument 2. Does not affect any
  verdict.
- Which of `176 SpawnAbility`'s three arguments is a type id. No presence verdict
  is claimed for `SpawnAbility`.
- Whether `0x8C`, `0x1E`, `0x9B`, `0x5E`, `0xEB`, `0x63`, `0x44` are in fact the
  portal / shockwave / bomb companions, or something else absent from GRAVE.
- Where the blob's sequence table lives exactly. `[0x28:0x2C]`, `[0x2C:0x30]`,
  `[0x30:0x34]` and `[0x34:0x38]` were measured to be count-prefixed tables of
  Script_Section byte offsets; which one is the sequence→anim-record map, if any
  of them is, was not resolved.
- Whether the level `.PSX` overrides member fields per placement. This is the
  leading un-excluded candidate for `0x49` and a live one for `0x53`. The `.PSX`
  was not decoded (out of scope, Req 2.5's named candidate).
- Whether ordinal 0/3/4's underlying anim records actually ported and rig-bound
  for `0x3A` and `0x0D`. That needs Static_Checks 1 and 2 (Task 15), not yet
  implemented.
- Rig binding was **not** evaluated for any case: `can_bind_to_rig` and
  `classify_rig_binding` belong to Task 15.
- Size_Delta: nothing was built (Task 21).
- `211 CreateExplosion`'s arguments were not traced beyond noting it is absent
  from `0x49`'s section and present once in `0x53`'s.

### Data safety for this run (Req 9.1, 9.2, 9.3, 9.5, 9.6, 9.7)

- Six analysis processes, each in a **fresh** terminal, each parsing **at most
  four** binaries; the five per-enemy processes each parsed exactly four.
- One `.PRS` resident at a time: source read, walked, `del`'d, then target.
- Every open was read mode. No temporary, scratch or intermediate file was
  created anywhere, including `%TEMP%`. No `.PRS`/`.PRT`/`.BEF`/`.PSX`/ELF and no
  disc image was written. `game_files/` was not modified.
- Post-run inventory: **101 files, 158,393,751 bytes** — identical count and
  identical byte total to Task 2's pre-run figure. Most recent mtime anywhere
  under `game_files/` is `2026-07-11 16:38:04` (`GRAVE.BEF`); **no file has been
  modified in the last 24 hours**, and this run took place on 2026-08-09.
- The aggregate hash figure differs from Task 2's because Task 2 did not record
  its concatenation formula. Pinned here so it is reproducible — aggregate over
  `sorted (relpath, sha256(content))` pairs joined as `name + hexdigest` with no
  separator: **`5b86e7a7261a0e93c651df0cf7033c89ad6a4bbf49b66ffff522cdbba2321f95`**.
  Count, byte total and mtimes all agree that the data is unchanged; the hash
  mismatch is a formula difference, not a data change.
