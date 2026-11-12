# `StreamMemoryEnd` — the SPU base, traced

RESEARCH ONLY. Nothing in `randomizer/` was touched, no constant was changed, no binary was
written, no test was run. This file is the only file created.

This document closes the blocker that `research/MJOLNIR_IRX_SOUND_DRIVER.md` and
`research/MULTI_WORLD_TARGET_BUDGET_MEASUREMENT.md` both recorded in the same words: *"do not
touch the sound path until the `StreamMemoryEnd` writers at 0x00141714 and 0x00141858 are
traced."*

It closes it by showing that **those two addresses have nothing to do with sound.** The base is
elsewhere, it is a literal, and it is now known.

---

## Answer first

| Question | Answer | Class |
|---|---|---|
| SPU base (`StreamMemoryEnd`) on the normal-play path | **0x0001DA10 = 121,360** | **MEASURED** — a literal `lui`/`ori` pair at VA 0x00194C5C/0x00194C64 |
| Set once at boot or per level? | **Per bank load**, always to the same literal | MEASURED |
| Does any reachable path enable reverb (IRX command 19)? | **No** | **CONFIRMED** — two independent lines of evidence |
| Usable SPU for a bank's sample block | **2,097,152 − 121,360 = 1,975,792 B** | Arithmetic on a MEASURED base and a structural 2 MB |
| Does that legalise UNDER and CASTLE's shipped banks? | **Yes**, with 4,144 B and 2,608 B to spare | Arithmetic |
| Does it buy meaningful import capacity? | **Barely.** +16,624 B in grave, +13,168 in swamp, +13,104 in ice, and in under/castle it converts a wholesale refusal into a near-zero budget | Arithmetic |

---

## Batching

Two binaries total, **one per process, never two resident at once**:

- **Process group A — `game_files/SLUS_200.17` alone.** Program headers, targeted
  disassembly windows, exhaustive `jal`/`j` call-graph scans, exhaustive scans for stores into the
  RPC packet buffer, and a data-word scan for function pointers. Several short processes, each
  opening exactly this one file.
- **Process group B — `game_files/SLPM_621.27` alone.** Same three checks re-run: the base
  literal, the RPC server bind, and the full command-number enumeration.

No `.BD`, `.BEF`, `.PRS`, `.PRT` or `.IRX` file was opened by this pass. Every bank size and
per-enemy sound cost quoted in §5 is carried over **as a literal** from the prior measurement
documents and from `tests/test_sound_bank_merge.py`, and is labelled with its origin.

Offsets: `file_offset = VA − 0x00100000 + 0x80` in both builds (single PT_LOAD, `p_vaddr`
0x00100000, `p_offset` 0x80). `SLUS_200.17` `p_filesz` 1,229,440 / `p_memsz` 1,991,296;
`SLPM_621.27` 1,259,392 / 2,021,120.

---

## 0. The prior docs were tracing the wrong RPC server

This has to come first, because everything downstream depends on it.

`MJOLNIR.IRX`'s `ProcessEECommand` tests `fno == 0` and nothing else
(`MJOLNIR_IRX_SOUND_DRIVER.md` §2, measured there). **The sound driver's SIF function number is
always 0. The IRX command number lives inside the packet, as an `s16`.** The prior pass recovered
`li $a1, n` before each `jal 0x0010FBF8` and read `n` as an IRX command number. `$a1` at that call
is the *SIF fno*, so the identification is a category error: it matched IRX command numbers
against the function numbers of whichever RPC server the call happened to address.

The helper's signature, MEASURED from its prologue at 0x0010FBF8 (EE ABI, eight register
arguments `a0-a3,t0-t3`), is `sceSifCallRpc`-shaped:

```
0010FC00  daddu $s1, $a0   ; a0 = client data (cd)
0010FC1C  daddu $s6, $a1   ; a1 = fno
0010FC14  daddu $fp, $a2   ; a2 = mode
0010FC24  daddu $s5, $a3   ; a3 = send
0010FC3C  daddu $s2, $t0   ; t0 = ssize
0010FC2C  daddu $s4, $t1   ; t1 = recv
0010FC34  daddu $s3, $t2   ; t2 = rsize
                           ; t3 = end_func, 0($sp) = end_para
```

Applying that to the three sites the prior docs named:

| site | `a0` (cd) | `a3` (send) | `t1` (recv) | `t2` |
|---|---|---|---|---|
| 0x00141714 | 0x00231780 | 0x00231830 | **0x00232D40** | 4 |
| 0x00141858 | 0x00231780 | 0x00231830 | **0x00232D40** | 4 |
| 0x00196844 | 0x002E2340 | **NULL** | **0x002E20C0** | 4 |

**`0x00232D40` and `0x002E20C0` are RECEIVE buffers, not send buffers.** That is why the prior
pass could not find literals writing `args[0]`/`args[1]` into them: nothing writes them, the IOP
does. And the client data at 0x00231780 / 0x002E2340 are not the sound client. The 1,044-byte
request at 0x00231830 whose tail is a 1,023-char NUL-terminated string is a *path*, so this is a
file/CD server; its fno 14 and fno 19 are that server's functions, unrelated to
`StreamMemoryEnd` and `SOUND_EnableEffects`.

**Consequence for task 1 as posed:** there is no instruction anywhere in either build that writes
`args[0]`/`args[1]` at 0x00232D40 or 0x002E20C0, because those are not argument blocks. The
question is answered by refuting its premise. The real argument words are traced in §2.

### The real sound client

One site in the whole image names RPC server 0x00012345:

```
001B7CD0  SoundBindRpc:
001B7CD8    jal sceSifInitRpc(0)
001B7CE4    a0 = 0x002E5000              ; the sound client data
001B7CEC    a1 = 0x00012345              ; lui 0x0001 / ori 0x2345
001B7CF0    jal sceSifBindRpc(cd, 0x12345, 0)
001B7D30    poll *(u32*)0x002E5024 until bound
```

and one function issues the call:

```
001B7D70  SoundCallRpc(fno, send):
001B7D74    t1 = 0x002E4980             ; recv, rsize 512
001B7D7C    t3 = 0x001B7D60             ; end_func
001B7D94    a0 = 0x002E5000             ; cd
001B7D98    t0 = 2048                   ; ssize
001B7DA0    jal 0x0010FBF8
001B7DB0    return *(u32*)0x002E4980
```

`SoundBindRpc` has exactly one caller (0x00194A5C); `SoundCallRpc` has exactly one caller
(0x001B85E4). Neither address appears as a data word anywhere in the image, so neither is
reachable indirectly.

---

## 1. The packet buffer, and why the command set is a closed enumeration

The flush function at 0x001B8590 is the only thing that ever transmits:

```
001B8590  SoundFlush(mode):
001B859C    jal 0x001B81E0                     ; drain previous
001B85A8    if sceSifCheckStatRpc(0x002E5000) return -1
001B85C4    nCommands = *(u16*)0x002E3980 ; if 0 return -2
001B85DC    a1 = 0x002E3980                    ; send = the packet
001B85E4    jal SoundCallRpc(fno = 0, send)    ; fno 0, exactly as the IRX expects
001B85F4    *(u16*)0x002E3980 = 0              ; reset count
```

The packet at **0x002E3980** is built by one appender, 0x001B8120:

```
001B8120  AddSoundCommand(cmd, nargs):
001B8134    if (cursor + nargs + 3) >= 1024 return          ; silent drop
001B8150    *(u16*)(0x002E3982 + 2*cursor) = cmd
001B8160    *(u16*)(0x002E3984 + 2*cursor) = nargs
001B817C..  copy nargs halfwords from the ARG STAGING AREA at 0x002E4180
001B8194        into 0x002E3980 + 2*cursor + 6
001B81C4    (*(u16*)0x002E3980)++                           ; nCommands
001B81CC    cursor += 2 + nargs                             ; in gp-30572
```

This matches the IRX's measured packet layout exactly (`s16 nCommands`, then
`s16 command, s16 nArgs, s16 args[nArgs]` per entry).

**An exhaustive scan of both builds for any instruction referencing 0x002E3980..0x002E39A2
(SLUS) returns 8 results, all inside 0x001B8120-0x001B81D4 and 0x001B8590-0x001B85F8.** Nothing
else in the executable writes the packet. So:

- every command number that can ever be sent is the `cmd` argument at one of the appender's call
  sites, and
- the appender is reached only by direct `j`/`jal` — 18 sites, and its address appears as a data
  word **zero** times, so there is no dispatch table and no `jalr` route into it.

The 18 sites, with the command literal recovered at each (MEASURED, both builds, identical set):

| SLUS site | cmd | SLPM site | cmd |
|---|---|---|---|
| 0x00194E9C | 10 | 0x0019518C | 10 |
| 0x00194ED4 | 11 | 0x001951C4 | 11 |
| 0x001B7DD4 | 6 | 0x001B80C4 | 6 |
| 0x001B7E70 | 1 | 0x001B8160 | 1 |
| 0x001B7EF8 | 0 | 0x001B81E8 | 0 |
| 0x001B7F4C | 2 | 0x001B823C | 2 |
| 0x001B7F84 | 31 | 0x001B8274 | 31 |
| 0x001B7FB0 | 5 | 0x001B82A0 | 5 |
| 0x001B7FFC | 4 | 0x001B82EC | 4 |
| 0x001B8034 | 30 | 0x001B8324 | 30 |
| 0x001B8054 | 9 | 0x001B8344 | 9 |
| 0x001B8064 | 7 | 0x001B8354 | 7 |
| 0x001B807C | 12 | 0x001B836C | 12 |
| 0x001B809C | 15 | 0x001B838C | 15 |
| 0x001B80EC | 13 | 0x001B83DC | 13 |
| **0x001B8118** | **14** | **0x001B8408** | **14** |
| 0x001B861C | 28 | 0x001B890C | 28 |
| 0x001B863C | 29 | 0x001B892C | 29 |

Command set = **{0, 1, 2, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 28, 29, 30, 31}**.

Commands **19, 20, 21, 22, 23, 24, 25, 26, 27** — the whole `SOUND_*Effects` block plus
`PatchExternalFile` and `ChainStreams` — are **never sent by either build**. The EE-side wrappers
for them do not exist; there is no unreachable wrapper to argue about, the code was simply never
written.

---

## 2. The command-14 wrapper, and the base

```
001B8100  SOUND_SetStreamMemoryEnd(u32 base):
001B8100    srl  $v0, $a0, 16
001B8108    sh   $v0, 0x002E4180        ; args[0] = base >> 16
001B8110    sh   $a0, 0x002E4182        ; args[1] = base & 0xFFFF
001B8114    a0 = 14
001B8118    j    AddSoundCommand(14, 2)
```

So `args[0]`/`args[1]` — the two halfwords the IRX recombines as
`StreamMemoryEnd = (args[0]<<16) | args[1]` — are written **at 0x001B8108 and 0x001B8110**, into
the argument staging area at 0x002E4180/0x002E4182, and copied into the packet by the appender's
loop at 0x001B817C-0x001B8194. That is the complete answer to task 1, relocated to the right
buffer. In `SLPM_621.27` the same wrapper is at 0x001B83F0 with the stores at 0x001B83F8 /
0x001B8400 and the staging area at 0x002EB640.

`SOUND_SetStreamMemoryEnd` has **exactly one caller in the entire image**, and it passes a
literal:

```
00194C5C    lui  $v0, 0x0001
00194C60    jal  0x001B8100              ; SOUND_SetStreamMemoryEnd
00194C64    ori  $a0, $v0, 0xda10        ; <-- $a0 = 0x0001DA10
00194C68    a0 = bankId
00194C6C    jal  0x001B7DC0              ; -> AddSoundCommand(6, ...) = load bank
00194C78    jal  0x001B8040              ; AddSoundCommand(9, 2) = status query
00194C80    jal  SoundFlush(0)
00194C88    jal  0x001B81E0              ; wait
00194CD4    spin while *(u32*)0x002E4B80 != 0   ; poll until the IOP reports done
```

`SLPM_621.27`, independently: `lui $v0,0x0001` / `jal 0x001B83F0` / `ori $a0,$v0,0xda10` at
0x00194F5C-0x00194F64, followed by the same command-6 / command-9 / flush sequence.

> ### `StreamMemoryEnd = 0x0001DA10 = 121,360` — MEASURED, a literal, identical in both builds.

It is **not** computed, so there is no chain to show and nothing is left unresolved. The prior
docs' caveat ("the SPU base the EE actually installs is still unknown") is discharged.

### Why the base is not zero

SPU RAM 0x000000-0x01DA10 is left below the bank. The IRX's `StartStream` uses
`StreamMemoryEnd` as the write cursor **only for the resident-bank case**; music streams get their
own SPU base (measured in `MJOLNIR_IRX_SOUND_DRIVER.md` §5.2). 121,360 B is the right order of
magnitude for the music double-buffer plus the SPU's own reserved low area. That this is what the
121,360 B is *for* is **INFERRED**; that the bank starts at 121,360 is MEASURED and is the only
part §5 rests on.

---

## 3. Which call sites run during normal level loading

The four sites the prior docs listed were the wrong server (§0), so this is the real call graph.
Every edge below is a direct `j`/`jal`; no `jalr`, no data pointer.

```
0x00147510   bank = *(u8*)($s5 + 0x13)   <-- the level-table row's sound-bank selector
0x0014766C   bank = $s3                  <-- boss-arena / caller-supplied id, bltz-guarded
0x00147888   bank = 220 (0xDC = LOGO.BD) <-- boot / logo
      |
      v  jal
0x0016C9A0   thunk: j LoadSoundBank(this + 0x6D24, bankId)
      |
      v  j (tail call)
0x00194C20   LoadSoundBank(psound, bankId):
               if (bankId < 208) return 0                      ; music ids rejected
               if (bankId == psound->residentId) return 1      ; single-bank cache
               SOUND_SetStreamMemoryEnd(0x0001DA10)   <-- HERE, unconditionally
               AddSoundCommand(6, bankId)
               AddSoundCommand(9, ...)
               SoundFlush(0); wait; poll
```

- **0x00147510 is the normal level-load site.** `*(u8*)($s5 + 0x13)` is the level-table row byte
  at `+0x13`, which prior work measured as the per-row sound-bank selector (0xD0-0xD4 for the five
  worlds, 0xD5-0xDA for boss arenas). It is immediately preceded by the same row's other
  level-setup reads. This site **executes on every level load**.
- **0x00147888 is the boot path** — bank 220, `LOGO.BD`.
- **0x0014766C** is the third, with the id in `$s3` from a function argument and guarded by
  `bltz $s3` (skipped when negative). It sits in the same level-load family.

**Therefore: the base is re-sent on every bank load, not set once at boot — and it is the same
literal 0x0001DA10 every time.** There is no per-level or per-world variation to discover. The
guard `if (bankId == psound->residentId) return 1` also re-confirms the single-resident-bank
restriction from a second place.

One consequence worth recording: because command 14 is re-sent immediately before every command
6, the IRX's post-load cursor advance
(`StreamMemoryEnd += roundup64(fileLength)`, measured in the IRX doc §5.5) is **discarded before
it can matter**. The cursor overshooting 2 MB after a large bank load is therefore harmless — it
is overwritten with 0x0001DA10 before the next transfer. The binding constraint is
`base + sample_block_bytes`, not the cursor.

---

## 4. The reverb inference, re-examined independently → **CONFIRMED**

I did not inherit the prior conclusion; I re-derived it, and it now rests on two independent
arguments, either of which is sufficient.

### Line 1 — the command set is closed and 19 is not in it (MEASURED)

Stated in full in §1. The chain is:

1. Only 0x001B8590 transmits, and it always sends `fno = 0` with `send = 0x002E3980`.
2. An exhaustive scan of both builds finds that **only** the appender and the flush touch
   0x002E3980..0x002E39A2. No other code path can forge a packet.
3. The appender has 18 call sites, all direct, and its address is never stored as data — so the
   **`jalr` concern the brief correctly raised is answered: there is no indirect route into it.**
   (For scale: the image contains 899 `jalr` instructions; none of them can reach this function,
   because its address is never materialised anywhere.)
4. At all 18 sites the command number is a literal, and 19 is not among them.

This is a closed enumeration, not an absence-of-callers argument. The prior pass's
"zero `jal` callers on a wrapper" reasoning — which was the genuinely fragile part, and which the
brief was right to distrust — is **not used here at all**. It was also attached to the wrong
function (a file-server fno-19 wrapper, §0).

### Line 2 — reverb-on is arithmetically impossible given the traced base (MEASURED + arithmetic)

With `base = 121,360` now known, assume reverb *were* enabled. The core-0 work area sits at
0x001FFFFF, so the usable region ends at 0x001E0000 = 1,966,080, leaving
`1,966,080 − 121,360 = 1,844,720` B for the sample block.

`GRAVE.BD`'s sample block is **1,890,448 B** (MEASURED,
`grave_bd_sound_reclamation_measurement.md` §1). That is **45,728 B more than would fit.**

Retail GRAVE works. So reverb is not enabled when a bank is resident. This line needs no call
graph at all — it falsifies reverb-on from the shipped data plus one traced literal.

### Verdict

**CONFIRMED: no reachable path enables reverb, and the top 128 KB of SPU RAM is not reserved.**

Residual scope, stated honestly: "CONFIRMED" means *no path in these two executables sends IRX
command 19*. It does not mean the SPU's top 128 KB is provably free of anything else — only that
*this* game's driver never places a reverb work area there. §5's structural top of 2,097,152
carries that one assumption, and §5 says where it could bite.

---

## 5. What `SPU_USABLE_CEILING` could become — arithmetic only

**No constant was changed.** Everything in this section is arithmetic; the labels say what each
input is.

### Inputs

| Input | Value | Class |
|---|---|---|
| SPU2 RAM | 2,097,152 (0x200000) | Structural (hardware) |
| `StreamMemoryEnd` base | 121,360 (0x1DA10) | **MEASURED**, §2 |
| Reverb reservation | none | **CONFIRMED absent**, §4 |
| Usable for the sample block | **1,975,792** | Arithmetic: 2,097,152 − 121,360 |
| Per-world sample-block sizes | below | **MEASURED** in prior docs, carried as literals |
| Per-enemy sound costs | below | **MEASURED**, `tests/test_sound_bank_merge.py` + reclamation doc |

### Per-world ceiling arithmetic

The IRX transfers **only the sample block** — `cdend1` subtracts the header length and advances
the IOP read pointer past it (MEASURED, IRX doc §4/§5.3). So the quantity that must fit is
`roundup64(sample_bytes)`, not `roundup64(file_size)`. This matters: it is the difference between
UNDER/CASTLE being legal and not.

| world | file size | header | sample bytes | `roundup64(samples)` | SPU end address | **headroom under 1,975,792** |
|---|---|---|---|---|---|---|
| GRAVE | 1,893,776 | 3,328 | 1,890,448 | 1,890,496 | 0x1EB0E0 | **85,296** |
| SWAMP | 1,952,156 | 3,436 | 1,948,720 | 1,948,736 | 0x1F9480 | **27,056** |
| ICE | 1,962,472 | 3,400 | 1,959,072 | 1,959,104 | 0x1FBCD0 | **16,688** |
| UNDER | 1,975,052 | 3,436 | 1,971,616 | 1,971,648 | 0x1FF7D0 | **4,144** |
| CASTLE | 1,976,728 | 3,592 | 1,973,136 | 1,973,184 | 0x1FF700 | **2,608** |

All five fit. **CASTLE, the largest retail bank, lands 2,608 B below the top of SPU RAM.** A
2,608-byte margin on a 2 MB budget is a 0.12% fit. That is strong corroboration, from a direction
entirely independent of the disassembly, that the base is 121,360, that the ceiling really is
2 MB, and that nothing else occupies the top of SPU RAM.

For comparison, the current constants: `SPU_USABLE_CEILING` = 1,966,080 compared against
`roundup64(file_size)`, giving grave 72,256 B of nominal room, of which
`HEADROOM_CONSERVATIVE` = 68,672 is used; UNDER (−9,024) and CASTLE (−10,688) fail outright.

### What the raise buys, against MEASURED per-enemy sound costs

MEASURED import costs into GRAVE: `0x49` **0** · `0x58` **10,240** · `0x16` **36,544** ·
`0x19` **43,056** · `0x0D` **45,248** · `0x27` **47,632** · `0x3A` **60,272** · `0x53` **66,032** ·
`0x5D` **86,192**.

| world | headroom today | headroom on the traced base | delta | cheapest-first arrivals today → on the traced base |
|---|---|---|---|---|
| GRAVE | 68,672 | 85,296 | +16,624 | `0x49`+`0x58`+`0x16` (46,784) → unchanged; `0x19` would need 89,840 |
| SWAMP | 13,888 | 27,056 | +13,168 | `0x49`+`0x58` (10,240) → unchanged; `0x16` would need 46,784 |
| ICE | 3,584 | 16,688 | +13,104 | `0x49` only (0) → **`0x49`+`0x58`** (10,240) |
| UNDER | refused wholesale | 4,144 | n/a | refused → `0x49` only (0 B imports) |
| CASTLE | refused wholesale | 2,608 | n/a | refused → still blocked, `nSFX` 299 |

With reclamation (MEASURED ceiling 82,032 B in grave, `grave_bd_sound_reclamation_measurement.md`
§1) grave's budget becomes ~167,328 B, which admits `0x49`+`0x58`+`0x16`+`0x19` = 89,840 and then
`0x0D` at 135,088 — four paid arrivals, one more than today. Reclamation figures for the other
four worlds have not been measured and are not estimated here.

### The honest headline

**The trace legalises UNDER and CASTLE's shipped banks. It does not meaningfully expand import
capacity anywhere.** The +9,712 B the ceiling itself moves (1,975,792 vs 1,966,080), plus the
3,328-3,592 B the header-vs-file semantics correction returns, is smaller than the cheapest
non-free enemy sound in swamp/ice and far smaller than any in under/castle. The value of this
trace is that two worlds stop being refused for a reason that was never true, and that the
remaining refusals become *correct* refusals resting on measured bytes rather than on a
conservative guess.

`CASTLE` remains permanently impossible regardless: `nSFX` = 299 against the driver's `< 300`,
which no byte budget can satisfy.

---

## 6. Recommendation (not applied)

If a later task chooses to act on this, the change is in `randomizer/sound_bank.py` and has two
parts, and **the second is the one that actually matters**:

1. `SPU_USABLE_CEILING`: 1,966,080 → **1,975,792**, restated as *"2 MB of SPU2 RAM minus the
   MEASURED `StreamMemoryEnd` base 0x0001DA10 (SLUS_200.17 VA 0x00194C5C/0x00194C64,
   SLPM_621.27 VA 0x00194F5C/0x00194F64); reverb is CONFIRMED never enabled — no reachable path
   sends IRX command 19 in either build, see research/STREAM_MEMORY_END_TRACE.md §4."*
2. **Compare `roundup64(sample_bytes)`, not `roundup64(file_size)`.** The header is parsed on the
   IOP and never transferred to SPU RAM (IRX doc §4, §5.3). Both `sound_port.merge_for_lineup`'s
   structural refusal and `research/tools/inspect_build_output.py`'s occupancy check currently use
   the file size, which over-charges every bank by its header. This is what makes UNDER and
   CASTLE fail; the ceiling number alone does not fix them.
3. `HEADROOM_CONSERVATIVE` = 68,672 and its documented "68,672-72,256 B measured band" become
   obsolete. The band's width was exactly the unknown base, and the base is now known: grave's
   free space is **85,296 B**, a single measured number with no band. `ceiling_for` should derive
   from the absolute ceiling rather than from `bank occupancy + headroom`; the comment block in
   `sound_port.py` at the `ceiling = sb.ceiling_for(...)` line already explains why the ceiling
   must be an absolute SPU limit computed before reclamation, and this change makes the code match
   that explanation instead of approximating it.

Also worth correcting when those files are next touched: the comment in
`sound_port.merge_for_lineup` that cites "the `StreamMemoryEnd` writers at SLUS_200.17 VA
0x00141714 / 0x00141858" as untraced. Those addresses belong to a different RPC server (§0) and
the real writer is 0x001B8100, called once, from 0x00194C60, with a literal.

**Whoever makes this change should keep the safety margin the project has always kept.** Nothing
here argues for spending the last byte: CASTLE's 2,608 B retail margin shows the hardware limit is
real and close, and the failure mode is still silent corruption.

---

## 7. What remains unknown, precisely

1. **Whether anything other than reverb occupies the top of SPU RAM.** §4 confirms *this driver*
   never places a reverb work area, and §5's CASTLE fit (2,608 B spare) is strong circumstantial
   evidence that the region is free. But absence of a reverb write is not proof that 0x1FF700
   onward is unused. To settle it: an emulator memory watch on SPU2 RAM 0x1F0000-0x200000 across a
   CASTLE level load, checking that nothing but the bank transfer writes there.
2. **What the 121,360 B below the base is for.** INFERRED as the music stream buffers plus the
   SPU's reserved low area. It does not affect §5 — the bank starts at 121,360 either way — but if
   music buffers were ever resized, the base literal would have to move with them.
3. **Per-world reclamation ceilings for swamp, ice, under, castle.** Only grave's (82,032 B) is
   measured. §5's arrival counts for the other four assume no reclamation.
4. **The third bank-load site at 0x0014766C.** Its `$s3` bank id was not traced to a source. It
   sends the same literal base, so it cannot change any §5 number; it is listed as unresolved only
   so the call graph in §3 is not read as complete.

If any of §5 is to be relied on for a shipped build, item 1 is the one to settle first, and a
memory watch at level load is the cheap way to do it.
