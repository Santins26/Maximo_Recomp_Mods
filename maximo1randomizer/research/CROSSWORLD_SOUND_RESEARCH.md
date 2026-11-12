# Cross-world ported enemies are silent — root cause

Research only. Nothing here is imported by any module and no game file was written.
Everything under "Measured" was read out of the real data in `game_files/`; everything
under "Inferred" is reasoning on top of it and is labelled as such.

## Verdict up front

The fix is **(d) infeasible with the current data set**, and the reason is structural,
not a missing remap.

* The sound operand is a **GLOBAL 16-bit Effect ID**, not a world-local index. There is
  nothing to remap.
* Sound data is **not** a `.PRS` resource category. There is nothing to port.
* Exactly **one** SFX bank is resident at a time, chosen by a single `u8` per level row,
  and the bank contents live on the IOP side behind a numeric ID that the EE executable
  never resolves to a filename. The bank files are not in `game_files/`.

## 1. The sound API slots (measured)

From the already-recovered API map in `randomizer/befscript.py`:

| slot | name | handler | arity | arg widths | tail call |
|---|---|---|---|---|---|
| 31 | `CScriptApi::PlaySoundEffect` | `0x00122B10` | 2 | `44` | `GetEngineObjectLink` |
| 195 | `CScriptApi::StopSoundEffect` | `0x00122BC0` | 1 | `4` | `PSound::StopEffect` |
| 208 | `CScriptApi::SetAnimateLandSound` | `0x00116CD0` | 2 | `44` | `FindAnimTrigger` |

Slot 31 is the one every enemy uses. Its two arguments, read in drain order, are
`(EffectID, volume)`; the volume literal is `128`, `63`, or similar in every observed site.

Call chain, disassembled from `MAXDEMOR.ELF` (debug build, full `.symtab`):

```
CScriptApi::PlaySoundEffect (0x00122B10)
  -> DEDynamicObject::PlaySound(int,int,bool)   0x0015BC80
    -> PSound::PlayEffect(int,int,uint)         0x0017C880
      -> SOUND_PlaySFX(...)                     0x0019F5A0   [IOP RPC]
```

`PSound::PlayEffect` asserts, against `PSound.cpp`:

* `EffectID >= 0 && EffectID <= 0xFFFF`
* `"Effect ID out of valid range"` for `EffectID >= 32766`
* `SFXCount < SFX_QUEUE_SIZE`

There is **no per-world array bound and no per-world base**. The ID is validated only
against a flat 16-bit space and then queued.

## 2. The operands, ported vs native (measured)

Decoded `Script_Section`s, retail revision, 100% coverage and clean decode in every case.

Ported and silent:

| world | type | name | `PlaySoundEffect` EffectIDs |
|---|---|---|---|
| SWAMP | `0x0D` | Zombie_Crocodile | 54, 430, 431, 432, 433, 434, 1367 |
| CASTLE | `0x16` | Axe_Guard | 404, 405, 406 |
| ICE | `0x1D` | Snowman | 345, 499, 856, 1367 |
| UNDER | `0x27` | Hammer_Devil | 364, 368, 369, 370, 1367 |

Native and audible, in GRAVE:

| type | name | EffectIDs |
|---|---|---|
| `0x47` | Basic_Skeleton | 716, 1445 (+1 non-literal, from a register) |
| `0x2F` | Axe_Skeleton | 114 (+1 non-literal) |
| `0x54` | Guard_Skeleton | 119, 120 (+2 non-literal) |

### The decisive observation

`GRAVE.BEF` already contains `0x0D` and `0x16` blobs that are **byte-identical** to
`SWAMP.BEF`'s and `CASTLE.BEF`'s, script section included — so vanilla GRAVE already
carries the literals 404-406 and 430-434. A world-local index space cannot produce that:
the same numeric operand appears verbatim in two different worlds' files. `1367` likewise
appears in SWAMP, ICE, UNDER and GRAVE.

**The operand is a GLOBAL id.** The index is fine; the sound data is what is missing.

Across all 70 clean GRAVE scripts the literal IDs span 1..1445 with 77 distinct values,
loosely clustered by world (GRAVE's own enemies in 76-208 and 700-1445, CASTLE's in 404-406,
SWAMP's in 430-434). Inferred: the space is partitioned by authoring convention so each
world's bank covers its own cluster.

## 3. Where the audio lives (measured)

**Not in `.PRS`.** Independently re-enumerated every material-section reference in all
five worlds' BEFs (`GRAVE`/`CASTLE`/`SWAMP`/`ICE` in one process, `UNDER` in a second, to
stay inside the four-binary-files-per-process rule):

| world | blobs | refs | rec_types | categories | flag bytes |
|---|---|---|---|---|---|
| GRAVE | 70 | 397 | `{0x05, 0x04}` | `{1, 2, 0}` | `{1, 2, 129, 130, 0}` |
| CASTLE | 80 | 494 | `{0x05, 0x04}` | `{1, 2, 0}` | `{1, 2, 129, 130, 0}` |
| SWAMP | 69 | 443 | `{0x05, 0x04}` | `{1, 2, 0}` | `{1, 2, 129, 130, 0}` |
| ICE | 74 | 415 | `{0x05, 0x04}` | `{1, 2, 0}` | `{1, 2, 129, 130, 0}` |
| UNDER | 66 | 440 | `{0x05, 0x04}` | `{1, 2, 0}` | `{1, 2, 129, 130, 0}` |

Only two rec_types and only categories 1 (`.PRS` MESH table) and 2 (`.PRS` ANIM table),
plus a null `flag=0, category=0` ref (4-6 per world). Flags 129/130 are the same two
categories with the global bit set. **There is no third resource category, so there is no
sound resource in the archives.** The PRS top-level resync gap never came into play — this
check does not walk PRS tables at all.

**`LevelFiles + 0x13` is confirmed as the SFX bank index.** In
`mglRunLevel(LevelFiles*, ...)`:

```
0x00129688  lbu $a1, 19($s5)            ; LevelFiles + 0x13
0x00129690  jal 0x0014F530              ; DarkEngine::LoadSoundEffects(int SoundBankID)
```

(`+0x12` is a signed byte feeding `PSound::PlayMusic` — the music track.)
`DarkEngine::LoadSoundEffects` is an 8-byte tail jump into
`PSound::LoadPrimarySoundBank(int)` at `0x0017C590`, which:

* rejects any `SoundBankID < 208`;
* returns early if `this+0x9F0` already equals the requested ID — i.e. it caches
  **one** resident bank;
* otherwise calls `SOUND_TransferSFX(SoundBankID, 0)` (`0x0019F4D0`), which writes the ID
  as a halfword into the IOP mailbox and fires RPC command 6.

The values in the real level table (31 rows, stride `0x34`, found via
`elf_patch.find_level_table`):

| bank | levels |
|---|---|
| 208 (`0xD0`) | GRAVE intro/hub/sub1/sub2/introB/bonus |
| 209 (`0xD1`) | ICESHIP intro/hub/sub1-3 |
| 210 (`0xD2`) | SWAMP intro/hub/sub1-3 |
| 211 (`0xD3`) | UNDER intro/hub/sub1-3 |
| 212 (`0xD4`) | CASTLE intro/hub/sub1/sub2 |
| 213-218 | the six boss levels, one bank each |

One `u8`, one bank, per level. **The SFX bank is per-world and fixed.**

**The bank files are not reachable.** Every data path string in the executable was
enumerated: `LPM62127\PSXDATA\{GRAVE,SWAMP,ICESHIP,UNDER,CASTLE,GAME,GAME2,MOVIES}\*.PSX`,
`GAME\{FONT,GLOBAL}.{PRS,PRT}`, `GAME2\GALLERY.{PRS,PRT}`, `psxdata\fx\fx.{prs,prt}`, and
the `PSXIRX\*.IRX` modules. There is **no sound-bank filename anywhere in the EE
executable** — the EE only ever sends the numeric ID. Inferred: `MJOLNIR.IRX` (the sound
driver) owns the bank paths and does the loading IOP-side. Naming the files would require
disassembling that IRX, which is out of scope here and would still leave the per-level
single-bank limit in place.

## 4. Why the enemies are silent

Measured facts chain cleanly: the ported enemy's script asks for global EffectID 404 (say);
GRAVE has bank 208 resident; bank 208 does not contain 404; `PSound::PlayEffect` validates
the ID against the flat 16-bit range, passes, enqueues it, and the IOP finds no sample.
Silence, with no assert and no crash — exactly the observed symptom.

(The "does not contain" step is the one inferred link. It is not directly observable
without the bank files, but it is the only explanation consistent with native enemies
sounding correct while foreign ones do not, given that the ID space is provably global.)

## 5. Fix shape

* **(a) index remap — impossible.** There is no per-world index to remap. A remap could
  only mean substituting a *different, GRAVE-resident* sound for the foreign one, which
  is not a fix but a re-scoring, and it would need a hand-authored per-enemy per-call-site
  mapping of 3-8 sites each with no way to audition the result from the repo.
* **(b) port sound resource data between `.PRS` archives — impossible.** Measured: no
  sound category exists in the archives. No byte estimate applies; the RESOURCE pool is
  not involved at all, so this costs nothing and buys nothing.
* **(c) requires files not extracted — true but not sufficient.** The banks are on the ISO
  under names only `MJOLNIR.IRX` knows. Even with them extracted, `LevelFiles+0x13` is a
  single `u8` and `LoadPrimarySoundBank` keeps exactly one bank resident, so you cannot
  have GRAVE's and CASTLE's SFX loaded simultaneously. Making this work would mean building
  merged custom banks per randomizer seed and teaching the IOP driver to load them — a
  different project, and one that writes new bank files to the ISO.
* **(d) infeasible / not worth it — this is the answer.**

## 6. What makes this not worth doing

The two blockers, both measured:

1. The SFX bank is **per-world and singular** — one `u8` field, one resident bank, a
   hard `>= 208` ID floor.
2. The EffectIDs are **baked as literals into compiled BEF script**, and while they *are*
   rewritable (the disassembler round-trips byte-exactly, so a literal push could be
   re-encoded), there is no correct value to write: no GRAVE-resident ID is the foreign
   enemy's sound.

Silent cross-world enemies are a cosmetic consequence of a fixed per-world audio bank.
They should be documented as a known limitation rather than chased.
