# SPU2 RAM 0x000000..0x01DA10 — what is down there, and what it would cost to take it

RESEARCH ONLY. Nothing in `randomizer/` was read for modification and nothing was modified. No
binary was written. No test was run. This file is the only file created; no temp or scratch file
was created anywhere.

This document answers the item `research/STREAM_MEMORY_END_TRACE.md` §7.2 left open: *"What the
121,360 B below the base is for. INFERRED as the music stream buffers plus the SPU's reserved low
area."* It is no longer inferred. The music buffer's SPU base and size are now MEASURED literals
in both EE builds, and they account for the region to the byte.

---

## Answer first

| Question | Answer | Class |
|---|---|---|
| Music buffer SPU base | **0x005010 = 20,496** | **MEASURED** — literal `addiu $a1,$zero,20496`, SLUS VA 0x00194A90 / SLPM VA 0x00194D90 |
| Music buffer half size | **0xC500 = 50,432** | **MEASURED** — literal `ori $a2,$zero,0xc500`, SLUS VA 0x00194A9C / SLPM VA 0x00194D9C |
| Music buffer total span in SPU RAM | **0x018A00 = 100,864** (2 × 50,432, double buffer) | MEASURED literals + arithmetic; the *double*-buffer reading is corroborated exactly (below) |
| Does it abut the bank base? | **Yes, exactly.** 0x005010 + 0x018A00 = **0x01DA10** = `StreamMemoryEnd` | Arithmetic on two independently measured literals — an exact match, not a near miss |
| Unaccounted-for space below the music buffer | **0x000000..0x005010 = 20,496 B**, of which the low ~2–4 KB is hardware-fixed | See §2 |
| Bytes with **no claimant identified anywhere in these three binaries** | **16,400** (0x001000..0x005010), or 18,448 if the hardware area is 2 KB not 4 KB | **MEASURED absence** of any claimant in the driver; **ASSUMED** hardware extent |
| Can `StreamMemoryEnd` be lowered on its own? | **No. Not by one byte.** The music double buffer's top is the byte immediately below it | MEASURED |
| Largest reclaim without touching music quality | **16,400 B** — slide the music buffer down to 0x001000 and lower the base by the same amount | Arithmetic; requires **two** literal edits per build, both or neither |
| Largest reclaim at all | **117,264 B** — music buffer removed entirely (no music) | Arithmetic |
| Does any of this make a full per-seed sound bill affordable? | **Only the most expensive option combined with grave reclamation**, and only the cheapest seed | §6 |

---

## Batching

Three binaries, **one resident per process, never two at once**, six short processes:

- **Process group A — `game_files/MJOLNIR.IRX` alone** (4 processes). Section-header and symbol-table
  dump (292 symbols, unstripped); targeted disassembly of `StartStream` @0x4B8 and
  `ADPCM_AllocIOPMemory` @0x29E8 and the command 12/13/14 dispatch arms @0x35F8-0x3658 and
  `AdpcmSdInit` @0x2A7C; and an **exhaustive `.text` scan** for every load/store whose 16-bit
  displacement is one of the four `StreamBuffer` SPU-address slots.
- **Process group B — `game_files/SLUS_200.17` alone** (2 processes). Disassembly of the command
  12/13/15 wrappers at 0x001B8070-0x001B80F0, an exhaustive `j`/`jal` scan of all 1,229,440 code
  bytes for callers of each, and disassembly of the single caller.
- **Process group C — `game_files/SLPM_621.27` alone** (1 process). Same two windows, to confirm
  the literals are identical.

No `.BD`, `.BEF`, `.PRS`, `.PRT` or ISO was opened. Every bank size and per-enemy sound cost in §6
is carried over **as a literal** from `research/STREAM_MEMORY_END_TRACE.md` §5 and
`research/grave_bd_sound_reclamation_measurement.md`, and is labelled there.

Offsets: IRX `file_offset = VA + 0x90`. EE `file_offset = VA − 0x00100000 + 0x80`.

---

## 1. The claimant, found

### 1.1 `StartStream` really does choose between two SPU bases (MEASURED)

`StartStream` @0x4B8 in `MJOLNIR.IRX`. Symbol table gives `StreamBuffer` @0x5F44 (164 bytes) and
`StreamMemoryEnd` @0x5FE8, so `*0x5F44` is `StreamBuffer[0]` — the SPU write cursor — and
`*0x5F9C` is `StreamBuffer+0x58`:

```
04D8  lw    $v1, 72($sp)          ; the 6th stacked arg: the resident marker
04DC  addiu $v0, $zero, 127
04EC  bne   $v1, $v0, 0x0508      ; not 127 -> music path
        ; --- resident bank path ---
04F8  lw    $v0, 0x5FE8($at)      ; StreamMemoryEnd
0504  sw    $v0, 0x5F44($at)      ; StreamBuffer[0] = StreamMemoryEnd
0500  b     0x0518
0508:   ; --- music path ---
050C  lw    $v0, 0x5F9C($at)      ; StreamBuffer+0x58
0514  sw    $v0, 0x5F44($at)      ; StreamBuffer[0] = that
```

That confirms the prior doc's sentence and localises the music base to exactly one variable,
`StreamBuffer+0x58` @0x5F9C.

### 1.2 Only one instruction in the whole IRX writes it (MEASURED, exhaustive)

An exhaustive scan of all 4,776 `.text` words for any load/store referencing 0x5F44, 0x5F98,
0x5F9C or 0x5FE8 returns 31 hits. **0x5F9C is written at exactly one address: 0x2A50, inside
`ADPCM_AllocIOPMemory`.** (0x5F98 likewise: one writer, 0x2A44, same function. 0x5FE8 has one
writer outside `StreamADPCM`: 0x361C, the command-14 arm. 0x5F44 is the cursor.)

So the music SPU base is set by **IRX command 13** and by nothing else. This answers the brief's
question about command 13 directly: **one of its arguments is an SPU address.**

### 1.3 `ADPCM_AllocIOPMemory` decoded (MEASURED)

Dispatch arm, 0x3620:

```
3620  addiu $a0, 13 ; bne ...
362C  a1 = args[1]; a2 = args[2]; v1 = args[3]; v0 = args[4]; a0 = args[0]
3640  a1 = (args[1]<<16) + args[2]
3648  a2 = (args[3]<<16) + args[4]
364C  jal 0x29E8   ADPCM_AllocIOPMemory(a0, a1, a2)
```

Body, 0x29E8:

```
29FC  if (*0x5F48 /* StreamBuffer+0x04, the IOP buffer ptr */ != 0)
          goto printf("ERROR: IOP Buffer already allocated for StreamBuffer %x", a0)
2A04  s2 = a1                     ; held
2A0C  s1 = a2
2A10  s0 = s1 << 1                ; = 2*a2
2A1C  v0 = AllocSysMemory(0, s0, 0)      ; IOP RAM, 2*a2 bytes
2A28  *0x5F48 (StreamBuffer+0x04) = v0   ; IOP buffer address
2A30  *0x5F90 (StreamBuffer+0x4C) = s1   ; a2         -- half size
2A3C  *0x5F94 (StreamBuffer+0x50) = s1>>1; a2/2      -- quarter
2A44  *0x5F98 (StreamBuffer+0x54) = s0   ; 2*a2      -- total buffer size
2A50  *0x5F9C (StreamBuffer+0x58) = s2   ; a1        -- MUSIC SPU BASE
```

Therefore, to settle the brief's sub-question precisely:

| command-13 argument | what it is |
|---|---|
| `args[0]` (`a0`) | **not an address at all** — used only as the `%x` of the "already allocated" error string. Dead on the success path. |
| `(args[1]<<16)｜args[2]` (`a1`) | **an SPU2 RAM address** — the music stream's SPU base. Feeds `StreamBuffer[0]`, the same slot `StreamMemoryEnd` feeds. MEASURED via §1.1/§1.2. |
| `(args[3]<<16)｜args[4]` (`a2`) | **a size, in bytes, in both address spaces at once** — `2*a2` bytes of **IOP** RAM are allocated for the CD double buffer, and `a2` is the chunk size the SPU transfers use. |

`AdpcmSdInit` (command 7) @0x2A7C was checked for a third claimant and has none: it calls
`sceSdInit(0)`, `sceSdSetParam(10, 2048)`, master volume ×2, and writes only IOP `.bss`. **No SPU
address literal anywhere in it.**

`InitStreamData` (command 12) @0x38A0 is called immediately before command 13 with `args[0] = 0`
(§1.4) and **takes no address argument at all** — its EE wrapper at 0x001B8070 stages a single
halfword. It is not a claimant on SPU RAM.

### 1.4 The EE side: one caller, three literals (MEASURED, both builds)

The command-13 wrapper, `SLUS_200.17` @0x001B80B0 — five args staged, exactly matching the IRX:

```
001B80B0  sh $a0, 0x2E4180        ; args[0]
001B80B8  sh (a1>>16), 0x2E4182   ; args[1]   \
001B80CC  sh (a1&0xFFFF), 0x2E4184; args[2]   / SPU base
001B80D4  sh (a2>>16), 0x2E4186   ; args[3]   \
001B80F0  sh $a2, 0x2E4188        ; args[4]   / size
001B80E4  a0 = 13 ; a1 = 5 ; j AddSoundCommand
```

An exhaustive `j`/`jal` scan of the whole SLUS image finds **exactly one caller** of this wrapper,
one of the command-12 wrapper and one of the command-15 wrapper. All three are in the sound-system
boot function:

```
00194A5C  jal SoundBindRpc                      ; binds RPC server 0x00012345
00194A80  jal 0x001B8060                        ; cmd 7  AdpcmSdInit
00194A88  jal 0x001B8070                        ; cmd 12 InitStreamData(0)
00194A8C  move  $a0, $zero
00194A90  addiu $a1, $zero, 20496               ; <-- 0x005010  MUSIC SPU BASE
00194A94  move  $a0, $zero
00194A98  jal 0x001B80B0                        ; cmd 13 ADPCM_AllocIOPMemory(0, 0x5010, 0xC500)
00194A9C  ori   $a2, $zero, 0xc500              ; <-- 50,432    HALF SIZE
00194AA0  jal SoundFlush(0)
```

`SLPM_621.27`, independently, byte-for-byte the same values at VA 0x00194D90 (`addiu $a1,$zero,
20496`) and 0x00194D9C (`ori $a2,$zero,0xc500`), calling the wrapper at 0x001B83A0.

The enclosing function is guarded at its head by `lw $v0,0($a0); beq $v0,$zero,...` → else
`printf("...already initialised")`, and it ends by storing state 2 into `*(u32*)$s0`. It is the
one-shot sound boot. `ADPCM_AllocIOPMemory` additionally refuses to run twice on the IOP side
(§1.3, the `*0x5F48 != 0` guard). **So the music base is set once, at boot, from a literal, and
never changes for the life of the run** — unlike `StreamMemoryEnd`, which is re-sent before every
bank load.

### 1.5 The arithmetic that closes it

```
music base            0x005010 =  20,496
music span  2 × 0xC500 = 0x018A00 = 100,864
                       ----------------------
top of music buffer   0x01DA10 = 121,360
StreamMemoryEnd       0x01DA10 = 121,360     <-- MEASURED separately, prior doc §2
```

**Exact.** Two literals measured from a different function, in a different address space, at a
different point in the boot, sum to the third literal with zero slack. That is the evidence that
the SPU-side music region really is `2 × a2` and not `a2` or `4 × a2`, and it is why the
double-buffer reading is stated as near-certain rather than as a guess. (If the SPU region were
only `a2` = 50,432, the base would sit at 0x011510 and 50,432 bytes would be unexplained; no other
combination of the measured literals lands on 0x01DA10.)

The wrap mechanics are consistent with it and are labelled INFERRED: `UpdateStream` computes
transfer targets as `StreamBuffer[0] + (a2/2)*index` (0x1E04-0x1E20, using `*0x5F94` = `a2/2`), and
the one place the cursor is advanced in place, `0x1E98-0x1EA4`, is on the resident-bank arm. I did
not exhaustively prove the music path never advances `StreamBuffer[0]`; the §1.5 sum is what the
map rests on.

---

## 2. What the hardware reserves, as distinct from what the game put there

**LABEL: this subsection is ASSUMED from general SPU2 hardware documentation. Nothing in
`MJOLNIR.IRX`, `SLUS_200.17` or `SLPM_621.27` measures it, and I did not find it stated in any of
the three binaries.** It is included because getting it wrong is the difference between a working
reclaim and silent audio corruption, and because the game demonstrably steers clear of it.

SPU2 keeps a small number of **fixed, non-relocatable** buffers at the very bottom of its 2 MB,
written by the hardware itself, not by any software allocator:

- the **external / CD audio input capture buffers**, left and right, for core 0;
- the same pair for core 1.

Each is one 0x200-byte block, giving **0x000000..0x0007FF = 2,048 B** as the figure I am most
confident of. Some documentation and some emulator implementations describe the reserved low area
as **0x000000..0x000FFF = 4,096 B** (the PS1 SPU additionally fixed voice-1 and voice-3 capture
blocks in the same region, and SPU2 inherits much of that layout). I cannot resolve 2 KB vs 4 KB
from these binaries.

**Treat 0x000000..0x000FFF (4,096 B) as hardware and not available.** The cost of the conservative
reading is 2,048 bytes out of 121,360 — 1.7% of the region and under 0.1% of a bank — and the cost
of the optimistic reading being wrong is silently crackling audio whenever CD audio capture is
active.

What the hardware does **not** reserve: everything from 0x001000 up. The reverb work areas are at
the *top* of RAM (0x001FFFFF / 0x001DFFFF, measured in `MJOLNIR_IRX_SOUND_DRIVER.md` §5) and are
CONFIRMED never enabled (`STREAM_MEMORY_END_TRACE.md` §4). So nothing at the bottom above 0x001000
is hardware's.

### Is the closure of claimants real?

Yes, with one honest hole. Every route by which an SPU2 address can be chosen in this system is:

| route | disposition |
|---|---|
| IRX command 14 → `StreamMemoryEnd` | MEASURED literal 0x01DA10, one caller |
| IRX command 13 → music base | MEASURED literal 0x005010, one caller, boot-only |
| per-voice `sceSdSetAddr` | **derived**, never independent: `entry.spuAddr = w1 + bankSpuBase` (IRX doc §4), i.e. always inside the bank's own block |
| IRX command 19 reverb work area | **CONFIRMED never sent** by either build (prior doc §4, closed enumeration of 18 literal command numbers) |
| `AdpcmSdInit` / `sceSdSetParam` / `sceSdSetCoreAttr` | checked, §1.3 — **no SPU address literals** |
| SPU2 hardware capture | fixed low blocks, §2, ASSUMED |

The hole: this enumerates claimants **in these three binaries**. The PS2 IOP BIOS, `libsd` itself,
and any other resident IRX the game loads are outside the three files opened. `libsd` is linked
into `MJOLNIR.IRX` only as import stubs; its own behaviour is not visible here. That is the reason
§4 stops short of declaring the 16,400 B provably free.

---

## 3. Address map, 0x000000..0x01DA10

| Range | Size | Claimed by | Evidence class |
|---|---|---|---|
| 0x000000..0x0007FF | 2,048 | SPU2 hardware: core 0 / core 1 external-input capture, 4 × 0x200 | **ASSUMED** (hardware documentation; not measured in any binary) |
| 0x000800..0x000FFF | 2,048 | possibly more hardware-fixed capture; **unresolved** | **ASSUMED / UNKNOWN** — 2 KB vs 4 KB reserved area could not be decided statically |
| 0x001000..0x00500F | **16,400** | **nothing identified.** No claimant in `MJOLNIR.IRX`, `SLUS_200.17` or `SLPM_621.27` | **MEASURED absence** within the three files (§2 table); not proof of freedom (§2 hole) |
| 0x005010..0x0157FF | 50,432 | **music stream buffer, half A** | **MEASURED** — base literal SLUS 0x00194A90, size literal SLUS 0x00194A9C |
| 0x015800..0x01DA0F | 50,432 | **music stream buffer, half B** | MEASURED literals; the *two-halves* split is INFERRED (§1.5 arithmetic + `*0x5F94` use) |
| **0x01DA10** | — | `StreamMemoryEnd`: base of the resident sample bank | MEASURED, prior doc §2 |

Total below the base: 2,048 + 2,048 + 16,400 + 50,432 + 50,432 = **121,360 = 0x01DA10.** The map
closes with no residue.

Note that **0x005010 is 16-byte aligned but not 64-byte aligned** (0x5010 mod 64 = 16), as is
0x01DA10. So the authors aligned to the SPU voice/transfer granularity of 16 bytes, not to the
64-byte granularity the driver uses for its post-load cursor rounding. That matters in §5.

---

## 4. How far can `StreamMemoryEnd` be lowered, and what breaks

**The headline the brief asked for, stated exactly as requested: the music buffer sits there, and
lowering the base alone would make the bank overwrite music. The music buffer is 100,864 bytes
(0x018A00), two halves of 50,432, occupying 0x005010..0x01DA0F, and its top byte is the byte
immediately below the bank base. `StreamMemoryEnd` cannot be lowered by even 16 bytes on its own.**

So the real lever is the music buffer, and each way of pulling it is a different change with its
own cost:

### Step 0 — lower the base only. Reclaim: 0 B. Breaks: music, immediately.

The bank's sample block would be written over the top of the music double buffer. What you would
hear: the bank loads fine, SFX play fine, and then the moment music streams into half B it
overwrites the top of the bank's samples — or the bank load overwrites music half B and music
plays as noise. Both are silent: no bounds check exists anywhere
(`MJOLNIR_IRX_SOUND_DRIVER.md` §5, "**no ceiling check at all**"), no log line, no test failure.
**Do not do this.**

### Step 1 — slide the music buffer down into the unexplained gap. Reclaim: **16,400 B**.

Set the command-13 base literal to 0x001000 and the command-14 base literal to
0x001000 + 0x018A00 = **0x019A00**. Music keeps its full 100,864 bytes, its full CD read-ahead, and
its exact behaviour; the whole assembly just slides down to sit on top of the conservative 4 KB
hardware floor.

- Reclaim: 0x005010 − 0x001000 = **16,400 B** (0x4010).
- Both new values are 16-byte aligned; 0x019A00 is 64-byte aligned, which is strictly better than
  vanilla.
- **What breaks if the §2 assumption is wrong:** if the hardware-reserved area actually extends
  above 0x001000, or if `libsd`/the IOP BIOS uses part of 0x001000..0x005010, music half A gets
  corrupted by hardware capture writes. Symptom: periodic ticking or garbage in the first ~50 KB of
  every music stream, worst when CD audio input is active. Silent — no error.
- **Risk assessment:** this is the *cheapest* reclaim and also the *least certain*. 16,400 B is 0.8%
  of SPU RAM and buys one extra enemy in grave (§6). Weigh that against a class of bug the test
  suite cannot see.
- Going further down, to 0x000800, would add 2,048 B for a straightforwardly worse risk profile.
  Not recommended; the 2 KB is inside the band §2 could not resolve.

### Step 2 — shrink the music buffer. Reclaim: 16,400 + (100,864 − 2·new_half).

The `ori $a2,$zero,0xc500` literal sets `a2`, and `a2` does **two** jobs (§1.3): `2*a2` bytes of
**IOP** RAM are `AllocSysMemory`'d for the CD double buffer, and `a2` sizes the SPU-side halves.
Halving it to 0x6280 = 25,216:

- Reclaim: 16,400 + 50,432 = **66,832 B** of SPU RAM, and incidentally 50,432 B of IOP RAM back.
- **What breaks:** CD read-ahead for music is halved. The driver refills a half while the other
  plays; with half the runway, any CD seek contention (a level load, an FMV, a `.PSX` read) that
  previously fitted in the slack now underruns. Symptom: music stutter, dropout, or the
  `"****** WARNING, WARNING!!...sceCdRead Error! ******"` / `"Would have been a stereo playback
  error!"` paths. This one at least *has* a log string, unlike the overflow cases.
- The failure is timing-dependent and therefore **not reproducible by static analysis or by any
  test in this repo.** It needs real hardware or emulator playtesting across a level load.
- Quarter-size (0x3140) would reclaim 16,400 + 75,648 = 92,048 B and is almost certainly too
  aggressive to ship.

### Step 3 — remove the music buffer entirely. Reclaim: **117,264 B**.

Set the command-13 base to 0x001000 and the command-14 base to 0x001000. Music then has nowhere to
stream to and must also be prevented from starting (command 1 / `USR_StartStream`), or it will
write straight into the bank.

- Reclaim: 121,360 − 4,096 = **117,264 B** (or 119,312 on the 2 KB reading).
- **What breaks: all music, in every level.** That is not a side effect, it is the change.
- Also note command 13 would still need a non-zero `a2` or `AllocSysMemory(0, 0, 0)` gets called;
  and the "already allocated" guard means the buffer can't simply be freed and re-made per level
  without also sending command 15.
- This is a large, visible, player-facing trade: the entire soundtrack for a handful of extra enemy
  voices. It is listed for completeness of the arithmetic, not as a recommendation.

### Is any of 0x000000..0x01DA10 "genuinely unused padding"?

**16,400 bytes have no identified claimant** (0x001000..0x00500F). I know this the way §2's table
knows it: an exhaustive scan of the IRX's 4,776 `.text` words found exactly one writer of the music
base and exactly one of `StreamMemoryEnd`; exhaustive `j`/`jal` scans of the full SLUS image found
exactly one caller of each wrapper; the reverb command is confirmed never sent; and per-voice
addresses are derived from a bank base rather than chosen. There is no fourth address source **in
these three files**.

What I cannot say is that those 16,400 bytes are *free*, because the IOP BIOS and `libsd`'s own
internals are not in these three files, and because §2's 2 KB-vs-4 KB question is unresolved. Why
would the authors leave a 16 KB hole immediately above the hardware area and start music at the
distinctly arbitrary 0x005010? Plausible answers range from "a 20 KB reserved pad they never
documented" to "0x5010 is just where a previous revision's buffer ended". **I do not know, and
guessing is exactly what the honesty requirement forbids here.**

### What it would take to settle it

An emulator memory watch on SPU2 RAM **0x000000..0x005010** across a level load with music
playing, with CD audio active, logging every write and its source. Specifically:

1. Break on any write to 0x001000..0x00500F. If nothing hits it across a full level load, a boss
   arena load, and a menu/FMV transition, Step 1 is safe.
2. Break on any write to 0x000800..0x000FFF, to decide the 2 KB-vs-4 KB question empirically.
3. While there, confirm the Step-1-relevant fact from the other direction: watch
   0x005010..0x01DA0F and check that the only writer is the music transfer, in two alternating
   50,432-byte halves. That upgrades the one INFERRED cell in §3 to MEASURED.

That is a single emulator session and it converts the whole of §4 from "arithmetic plus a
plausible absence" into measurement. **It should happen before any of this is shipped.**

---

## 5. Patch shape, if a reclaim is chosen (RECOMMENDATION ONLY — nothing implemented)

The change is **four literals across two executables**, and it has the same "both words or
neither" character as `elf_patch.patch_resource_pool`'s two-site write, for the same reason: a
half-applied patch is silent memory corruption, not a visible failure.

### The four edit sites

| Build | VA | file offset | encoding | field | vanilla |
|---|---|---|---|---|---|
| SLUS_200.17 | 0x00194A90 | **0x00094B10** | `addiu $a1,$zero,imm16` | music SPU base | 20496 (0x5010) |
| SLUS_200.17 | 0x00194A9C | **0x00094B1C** | `ori $a2,$zero,imm16` | music half size | 0xC500 |
| SLUS_200.17 | 0x00194C5C | **0x00094CDC** | `lui $v0,imm16` | bank base, **high** half | 0x0001 |
| SLUS_200.17 | 0x00194C64 | **0x00094CE4** | `ori $a0,$v0,imm16` | bank base, **low** half | 0xDA10 |
| SLPM_621.27 | 0x00194D90 | **0x00094E10** | `addiu $a1,$zero,imm16` | music SPU base | 20496 |
| SLPM_621.27 | 0x00194D9C | **0x00094E1C** | `ori $a2,$zero,imm16` | music half size | 0xC500 |
| SLPM_621.27 | 0x00194F5C | **0x00094FDC** | `lui $v0,imm16` | bank base, high half | 0x0001 |
| SLPM_621.27 | 0x00194F64 | **0x00094FE4** | `ori $a0,$v0,imm16` | bank base, low half | 0xDA10 |

### Which halves are editable, and the encodability rules

- **Bank base (`StreamMemoryEnd`) — both halves editable, any 32-bit value.** `lui`+`ori` is a full
  32-bit materialisation with no sign extension (`ori` zero-extends), so unlike
  `patch_resource_pool` — where only the `lui` half is editable and the low 16 bits are therefore
  forced to zero — **here the low 16 bits are freely settable.** No "not encodable" refusal class
  is needed for this field.
- **Music base — `addiu`, ONE halfword, SIGN-EXTENDED.** Legal values are 0x0000..0x7FFF only;
  0x8000 and above would be materialised as a negative number and the SPU address would be
  garbage. The vanilla 0x5010 and every value Step 1 or Step 3 would want are inside that range, so
  the constraint does not bind in practice — but a naive "write any 16-bit base here" helper is a
  bug waiting to happen and should refuse ≥ 0x8000 explicitly. Raising the music base above 0x7FFF
  would require replacing the instruction with a `lui`/`ori` pair, which there is no room for
  without relocating code. **Do not.**
- **Music half size — `ori`, ONE halfword, zero-extended.** Legal 0x0000..0xFFFF. Vanilla 0xC500
  is near the top of that range, so the size can be shrunk freely but essentially cannot be grown.

### Invariants any patcher must enforce, or refuse

1. **`bank_base == music_base + 2 * music_half`, exactly.** Greater wastes SPU RAM silently;
   **less overlaps, and overlap is silent corruption of either music or samples.** This is the
   invariant that makes the write all-or-nothing.
2. **`music_base >= 0x1000`** (§2's conservative hardware floor). Refuse below it.
3. **Both values 64-byte aligned** — stricter than the hardware's 16-byte need, matching the
   driver's own `roundup64` cursor granularity, and free to satisfy. Note this means the patched
   layout is *better* aligned than vanilla's 0x5010/0x1DA10.
4. **`2 * music_half` must not be reduced without a playtest** (§4 Step 2). A code-level check
   cannot catch an underrun.
5. **Both builds, or neither.** The randomizer patches whichever executable is present; a build
   that patched SLUS and not SLPM would be fine, but a build that patched the bank base in one file
   and the music base in the other would not be. Keep the four writes per file atomic.
6. **`SPU_USABLE_CEILING` in `randomizer/sound_bank.py` must move with it**, to
   `2,097,152 − new_bank_base`. If the literal moves and the constant does not, the merge budget is
   simply wrong, and wrong in the generous direction — which produces exactly the silent overflow
   this whole line of research exists to avoid.

The locate-by-signature approach `patch_resource_pool` uses transfers directly: the four sites are
each a unique instruction word in a unique context (`addiu $a1,$zero,20496` immediately preceding a
`jal` to the command-13 wrapper, etc.), and a re-run should accept a slot already holding one of our
own values as located.

---

## 6. Payoff arithmetic

Inputs, all carried as literals from prior MEASURED work (`STREAM_MEMORY_END_TRACE.md` §5,
`grave_bd_sound_reclamation_measurement.md` §1):

- Grave free space today: **85,296 B**.
- Grave reclamation ceiling: **82,032 B** (measured; the other four worlds' are not measured).
- Per-enemy import costs into grave: `0x49` **0** · `0x58` **10,240** · `0x16` **36,544** ·
  `0x19` **43,056** · `0x0D` **45,248** · `0x27` **47,632** · `0x3A` **60,272** · `0x53` **66,032** ·
  `0x5D` **86,192**. Nine enemies; cheapest-first cumulative totals:

| enemies | cumulative cost |
|---|---|
| 1 (`0x49`) | 0 |
| 2 (+`0x58`) | 10,240 |
| 3 (+`0x16`) | 46,784 |
| 4 (+`0x19`) | 89,840 |
| 5 (+`0x0D`) | 135,088 |
| 6 (+`0x27`) | 182,720 |
| 7 (+`0x3A`) | 242,992 |
| 8 (+`0x53`) | 309,024 |
| 9 (+`0x5D`) | 395,216 |

### Without reclamation

| option | reclaim | grave budget | enemies afforded | delta |
|---|---|---|---|---|
| today | — | 85,296 | **3** | — |
| Step 1 (slide music to 0x1000) | 16,400 | 101,696 | **4** | **+1** |
| Step 2 (halve music buffer) | 66,832 | 152,128 | **5** | +2 |
| Step 2 quarter | 92,048 | 177,344 | **5** | +2 |
| Step 3 (no music) | 117,264 | 202,560 | **6** | +3 |

### With grave reclamation (82,032 B) stacked on top

| option | grave budget | enemies afforded | delta vs 167,328 baseline |
|---|---|---|---|
| reclamation only | 167,328 | **5** | — |
| + Step 1 | 183,728 | **6** | +1 |
| + Step 2 half | 234,160 | **6** | +1 |
| + Step 3 | 284,592 | **7** | +2 |

### Against a full per-seed bill (226,096–330,240 B)

- Step 1 alone (101,696) is short of even the cheapest full bill by **124,400 B**.
- Step 3 alone (202,560) is short of the cheapest full bill by **23,536 B** — close, and still a
  refusal.
- **Only reclamation + Step 3 (284,592 B) clears the cheapest full bill (226,096).** It does not
  clear the most expensive (330,240), falling **45,648 B** short.

### The honest headline

**No.** Reclaiming the low region does not make all enemies audible, under any option, in any
combination measured here.

The cheap, quality-neutral option — Step 1, sliding the music buffer down onto the hardware floor —
buys **16,400 B and exactly one extra audible enemy in grave**, and it buys that by spending a
16 KB hole nobody can currently explain. Getting to "all enemies in a seed" requires deleting the
soundtrack *and* full bank reclamation, and even then only for the cheapest seeds.

Read against the prior doc's conclusion, the shape of the problem has not changed: the sound budget
is short by an amount comparable to a whole enemy's samples, and the remaining levers are each
worth about one enemy. The 121,360 B below the base was the largest remaining pool of space, and
now that it has been opened, **86% of it turns out to be the music buffer** — real, in use, and not
free. The genuinely unclaimed remainder is 16,400 B, 13.5% of the region and 0.8% of SPU RAM.

---

## 7. What remains unknown, precisely

1. **Whether 0x001000..0x00500F is actually free.** No claimant in the three binaries opened
   (MEASURED absence, exhaustive within those files), but `libsd` internals and the IOP BIOS are
   outside them, and the reason the authors started music at 0x005010 rather than lower is not
   recorded anywhere in the three files. **This is the single blocker on Step 1.** Settle it with
   the memory watch in §4, not by reasoning.
2. **2 KB or 4 KB of hardware-reserved low RAM.** ASSUMED, unmeasured, costs 2,048 B if answered
   conservatively. §4 step 2 of the watch decides it.
3. **That the SPU-side music region is two halves of `a2` rather than some other partition of
   `2*a2`.** The §1.5 sum makes the total certain; the split is INFERRED. Irrelevant to every
   number in §4 and §6 (which depend only on base and total), relevant only if someone tries to
   shrink the buffer asymmetrically.
4. **Whether shrinking `a2` (Step 2) actually underruns.** Timing-dependent, not decidable
   statically or by this repo's tests. Needs playtesting across a level load and an FMV.
5. **Per-world reclamation ceilings for swamp, ice, under, castle.** Still only grave's is
   measured, so §6's second table exists for grave alone.
6. **Whether command 15 (`FreeSysMemory` of the stream buffer, one caller at SLUS 0x00194B28) is
   ever reached at runtime.** Not traced. If it is, and command 13 is re-sent afterwards with
   different literals from somewhere, the map would need revisiting — but there is only one
   command-13 caller in the image and it carries the same literals, so this cannot change any
   number above. Listed only so §1.4 is not read as a complete lifecycle.

Final restatement of the risk, because it governs everything above: **the driver has no ceiling
check of any kind.** Every wrong answer in this document produces a build with corrupted or missing
audio and **no error, no log line and no test failure.** Items 1 and 2 are cheap to settle and
should be settled before a byte is patched.
