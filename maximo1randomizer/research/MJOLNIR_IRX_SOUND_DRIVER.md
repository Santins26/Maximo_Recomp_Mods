# MJOLNIR.IRX — IOP sound driver analysis

Research only. No behaviour changed, nothing in `randomizer/` touched.

Binaries opened by the analysis processes: **`game_files/MJOLNIR.IRX` only** (1 of the 4-file
budget). The EE executable was *not* re-opened; every EE-side fact quoted here is carried over
from `CROSSWORLD_SOUND_RESEARCH.md` and is labelled as prior work.

Every offset below is a **VA in the loaded IRX image** (`.text` starts at VA 0, `.data` at VA
0x4AA0). File offset = VA + 0x90 for the whole loaded image.

---

## 0. Disassembler sanity (MEASURED, before trusting any listing)

`research/tools/mips_disasm.py` decodes this image correctly. Evidence:

- Of 4776 `.text` words, 4738 decode to plain MIPS I. 35 decode to `.word` placeholders
  (COP0 `op=0x10` ×13, plus scattered reserved-on-R3000 opcodes) and 3 to `.mmi`
  (`op=0x1c`). That is 0.8%, consistent with COP0 interrupt code plus a few literal-data
  words embedded in `.text`. No `lq`/`sq` appeared, as expected for MIPS I.
- Independent cross-checks that the decode is aligned and correct: every function begins with a
  canonical `addiu $sp,$sp,-N` / `sw $ra` prologue and ends `jr $ra` / `addiu $sp,$sp,+N`;
  **every** decoded `jal` target lands exactly on a symbol-table function address; and every
  decoded `lui`/`addiu` pair coincides with an R_MIPS_HI16/LO16 pair in `.rel.text`.

So the listings are trustworthy. Nothing in the conclusions below rests on an undecoded word.

## 1. IRX structure (MEASURED)

| Property | Value |
|---|---|
| File size | 44,928 bytes (0xAF80) |
| ELF | `e_type` 0xFF80 (IOP relocatable), `e_machine` 8 (MIPS), `e_entry` 0x46D8 (`start`), `e_flags` 1 |
| Program headers | 2: `.iopmod` (0x70000080) at 0x74, and one PT_LOAD at file 0x90, filesz 22064, **memsz 33824** |
| Sections | `.text` 19,104 @VA 0 · `.data` 2,960 @VA 0x4AA0 · `.bss` @VA 0x5630 (11,760 = memsz−filesz) · `.rel.text` 13,944 (1743 relocs) · `.rel.data` 304 · **`.symtab` 292 symbols** · `.strtab` · `.comment` |
| `.iopmod` raw | `ffffffff d8464000 20d60000 a04a0000 900b0000 00000000 000000` → moduleinfo −1, entry 0x46D8, gp 0xD620, text_size 0x4AA0, **version 0x0B90** |
| Module name | **absent** — the name field in `.iopmod` is empty (the module is identified only by its RPC server id) |
| Toolchain | `.comment` = `MW MIPS C Compiler (2.4.1.01)` + `PlayStation2_IOP` (Metrowerks) |

The image ships a **full, unstripped symbol table with 292 named symbols**. That is why this
analysis is unusually direct — the function names below are the developers' own, not guesses.

### Imported IOP libraries (from the `*_stub` symbols and the library-name strings)

`libsd` (0x47A0), `sysmem` (0x481C), `intrman` (0x4848), `ioman` (0x4874), `loadcore` (0x48B0),
`sifcmd` (0x48D4), `sifman` (0x4910), `stdio` (0x493C), `sysclib` (0x4960), `thbase` (0x498C),
`thsemap` (0x49C8), `timrman` (0x49FC), `cdvdman` (0x4A38).

Imported entry points used: `sceSdInit`, `sceSdSetParam`, `sceSdGetParam`, `sceSdSetSwitch`,
`sceSdGetSwitch`, `sceSdSetAddr`, `sceSdGetAddr`, `sceSdSetCoreAttr`, `sceSdVoiceTrans`,
`sceSdSetTransCallback`, `sceSdSetEffectAttr`, `sceSdClearEffectWorkArea`; `sceSifInit`,
`sceSifCheckInit`, `sceSifInitRpc`, `sceSifSetRpcQueue`, `sceSifRegisterRpc`, `sceSifRpcLoop`;
`sceCdInit`, `sceCdRead`, `sceCdSeek`, `sceCdSync`, `sceCdStatus`, `sceCdDiskReady`,
`sceCdGetError`, `sceCdSearchFile`, `sceCdMmode`, `sceCdCallback`; `open`/`read`/`lseek`/`close`;
`AllocSysMemory`/`FreeSysMemory`; thread and hard-timer services; `printf`, `memcpy`, `strncmp`,
`FlushDcache`.

### Exported / entry points

`start` @0x46D8 is the only ELF entry. There is no IOP export table; the module's public
interface is the SIF RPC server. `sce_adpcm_loop` @0x2B48 is the server thread.

## 2. The RPC dispatcher (MEASURED)

`sce_adpcm_loop` @0x2B48 registers **RPC server id 0x00012345** with
`sceSifRegisterRpc(&sd, 0x12345, ProcessEECommand, rpc_arg /*2048 bytes @0x61EC*/, ...)`.

`ProcessEECommand(fno, buf)` @0x2AF8 is a two-line trampoline:

```
if (fno == 0)  AdpcmSetParam(buf);
else           printf("Driver error: unknown command %d \n", *(int*)buf);
```

So the SIF *function number* is always 0. **The real command number lives inside the packet**,
and `AdpcmSetParam` @0x339C (1608 bytes) is the command switch.

### Packet format (MEASURED from `AdpcmSetParam` head)

All fields are 16-bit:

```
s16 nCommands                      ; loop count, must be > 0
repeat nCommands times:
    s16 command
    s16 nArgs
    s16 args[nArgs]                ; copied into a 512-byte local frame, then dispatched
```

### Command switch — every case, with the VA of its compare

| Cmd | VA | Handler |
|---|---|---|
| 0 | 0x3734 | **`PlayResidentSFX(args[0]=EffectID, args[1]=channel, args[2], args[3], args[4])`** |
| 1 | 0x365C | `USR_StartStream(args[1],args[2],args[3],args[0], args[4..7])` — music |
| 2 | 0x37FC | `SetChannelVolume` |
| 3 | 0x3824 | `SetChannelPitch` |
| 4 | 0x37B0 | `sceSdSetSwitch` (key on/off) |
| 5 | 0x377C | `StopStream` |
| **6** | **0x36D8** | **`TransferResidentSampleBank(args[0]=bankId, args[1])`** |
| 7 | 0x3880 | `AdpcmSdInit` |
| 9 | 0x3410 | status query (`GetChannelStatus` + `AdpcmGetStatus`) |
| 10 / 11 | 0x3848 / 0x3864 | `PauseAll` / `ResumeAll` |
| 12 | 0x38A0 | `InitStreamData` |
| 13 | 0x3620 | `ADPCM_AllocIOPMemory(args[0], (args[1]<<16)|args[2], (args[3]<<16)|args[4])` |
| 14 | 0x35F8 | **`StreamMemoryEnd = (args[0]<<16) | args[1]`** — sets the SPU RAM cursor |
| 15 | 0x35AC | `FreeSysMemory` of the IOP stream buffer |
| 19 / 20 / 21 | 0x34A0 / 0x34E4 / 0x3504 | `SOUND_EnableEffects` / `DisableEffects` / `SetEffectMasterVolume` |
| 22 / 23 | 0x352C / 0x354C | `SOUND_AllChannelEffectsOn` / `Off` |
| 24 / 25 | 0x356C / 0x358C | `SOUND_SetChannelEffectOn` / `Off` |
| 26 | 0x346C | `SOUND_PatchExternalFile` |
| 27 | 0x3448 | `SOUND_ChainStreams` |
| 28 | 0x38C0 | `FadeMusicVolume` |
| 29 / 30 / 31 | 0x38E0 / 0x3914 / 0x393C | master switch / channel switch / music volume |

This confirms the prior EE-side measurement exactly: **command 6 is the bank loader** and the
`SOUND_PlaySFX` path is **command 0**.

### What command 6 does with the bank id

`TransferResidentSampleBank(id, x)` @0x2E68:

```
for (i = 0; ; i++, e += 16) {                 // e = ADPCM_FileInfo
    if (e->id != id) { if (e->id == -1) return 1; continue; }   // 1 = id not in table
    if (e->lsn == 0) return 2;                                  // 2 = file not found on disc
    CD_SPOT_FILE_ID = i;
    SpotFX_LoadAndTransfer(id, x);
    return 0;
}
```

`SpotFX_LoadAndTransfer(id, x)` @0x2EEC: rejects `x >= 300`, raises `SlotCount` to `x+1`, then

```
USR_StartStream(0, 0, 0, id, /*stack*/ 0, 127, 0, 0);
```

The literal **127** in the 6th argument is the "this is a resident sample bank, not music"
marker — `USR_StartStream` @0x388 tests for it and sets the resident flag at `StreamBuffer+18`.

## 3. THE KEY QUESTION — how a bank id becomes data

**Answer (b): the IRX holds a static table of absolute ISO paths indexed by id.**

`ADPCM_FileNames` @VA 0x4FE8, 304 bytes, **8 bytes per entry** = `{const char *path; s32 id;}`,
terminated by `id == -1`. `InitADPCMFiles` @0x270C walks it (stride 8, max 100 entries), and for
each entry `strncmp(path,"cdrom0:",7)` → `sceCdSearchFile` to resolve the LSN, filling
`ADPCM_FileInfo` (16 bytes/entry: `s16 id`, `u32 length`, `u32 lsn`, `u32 onCd`). A missing file
logs `"(%x) couldn't find lsn for file %s"` and leaves `lsn == 0`, which is the `return 2` above.

### The 13 sound-bank files, exact and complete (MEASURED)

Ids 0–23 are the music `.VGM` streams; ids **208–220 (0xD0–0xDC) are the SFX banks**:

| Id | Path | Name string VA / file offset |
|---|---|---|
| 208 | `cdrom0:\PSXDATA\SOUND\GRAVE.BD;1` | 0x4E10 / 0x4EA0 |
| 209 | `cdrom0:\PSXDATA\SOUND\ICE.BD;1` | 0x4E34 / 0x4EC4 |
| 210 | `cdrom0:\PSXDATA\SOUND\SWAMP.BD;1` | 0x4E54 / 0x4EE4 |
| 211 | `cdrom0:\PSXDATA\SOUND\UNDER.BD;1` | 0x4E78 / 0x4F08 |
| 212 | `cdrom0:\PSXDATA\SOUND\CASTLE.BD;1` | 0x4E9C / 0x4F2C |
| 213 | `cdrom0:\PSXDATA\SOUND\BS_KING.BD;1` | 0x4EC0 / 0x4F50 |
| 214 | `cdrom0:\PSXDATA\SOUND\BS_QUEEN.BD;1` | 0x4EE4 / 0x4F74 |
| 215 | `cdrom0:\PSXDATA\SOUND\BS_UNDER.BD;1` | 0x4F08 / 0x4F98 |
| 216 | `cdrom0:\PSXDATA\SOUND\BS_SWAMP.BD;1` | 0x4F2C / 0x4FBC |
| 217 | `cdrom0:\PSXDATA\SOUND\BS_ICE.BD;1` | 0x4F50 / 0x4FE0 |
| 218 | `cdrom0:\PSXDATA\SOUND\BS_GRAVE.BD;1` | 0x4F74 / 0x5004 |
| 219 | `cdrom0:\PSXDATA\SOUND\DEATH.BD;1` | 0x4F98 / 0x5028 |
| 220 | `cdrom0:\PSXDATA\SOUND\LOGO.BD;1` | 0x4FBC / 0x504C |

The 208→GRAVE, 209→ICE, 210→SWAMP, 211→UNDER, 212→CASTLE assignment matches the prior EE-side
level-table measurement exactly, and the boss ids 213–218 resolve to named boss banks.
Sentinel entry: path `"END OF LIST"` @0x4FDC with id `0xFFFFFFFF`.

Music paths (ids 0–23, VA 0x4AA0–0x4E0F): `TITLE`, `GRAVE1`, `GRAVE2`, `GRAVEHUB`, `ICE1`,
`ICE2`, `ICEHUB`, `SWAMP1`, `SWAMP2`, `SWAMPHUB`, `UNDER1`, `UNDER2`, `UNDERHUB`, `CASTLE1`,
`CASTLE2`, `CASTLEHB`, `CREDITS`, `G_BOSS`, `I_BOSS`, `S_BOSS`, `U_BOSS`, `C_BOSS1`, `C_BOSS2`,
`SORC` — all `cdrom0:\PSXDATA\MUSIC\*.VGM;1`.

### Every remaining string in the image, with VA / file offset

Library names: `libsd` 0x47A0/0x4830 · `sysmem` 0x481C/0x48AC · `intrman` 0x4848/0x48D8 ·
`ioman` 0x4874/0x4904 · `loadcore` 0x48B0/0x4940 · `sifcmd` 0x48D4/0x4964 ·
`sifman` 0x4910/0x49A0 · `stdio` 0x493C/0x49CC · `sysclib` 0x4960/0x49F0 ·
`thbase` 0x498C/0x4A1C · `thsemap` 0x49C8/0x4A58 · `timrman` 0x49FC/0x4A8C ·
`cdvdman` 0x4A38/0x4AC8.

Diagnostics: `END OF LIST` 0x4FDC · `Can NOT allocate hard timer ...` 0x51C0 ·
`Can NOT set timeup timer handler ...` 0x51E4 · `Can NOT setup hard timer ...` 0x520C ·
`Can NOT start hard timer ...` 0x522C · `cd seeking..error? %d` 0x524C ·
`****** WARNING, WARNING!!...sceCdRead Error! ******` 0x5264 ·
`Tray opened? Kicked PS2?...` 0x529C · `Would have been a stereo playback error!` 0x52BC ·
`file not found` 0x52E8 · `****CD Initialision failed.****` 0x52F8 · `cdrom0:` 0x531C ·
`host0:` 0x5324 · `(%x) couldn't find lsn for file %s` 0x532C ·
`ERROR: IOP Buffer already allocated for StreamBuffer %x` 0x5350 ·
`Driver error: unknown command %d ` 0x538C · `SpotFX_LoadAndTransfer(%i, %i)` 0x53B0 ·
`USR_StartStream returned %i` 0x53D0 ·
`Invalid sound effect id: %i channel: %i  Call ignored` 0x53F0 ·
`IOP received PAUSE message` 0x5428 · `buffer %x not allocated` 0x5444 ·
`TransferResidentSampleBank returned %i` 0x5460 · `errornumber %d...` 0x5488 ·
`sceCdGetError() function issue failed` 0x549C · `No error` 0x54C4 ·
`Outermost track reached during playback` 0x54D0 · `Cover opened during playback` 0x54FC ·
`Problem occured during read` 0x551C · `Not appropriate for disc in drive` 0x553C ·
`Processing command` 0x5560 · `Abort command received` 0x5574 · `unknown error` 0x558C ·
`CAN NOT CHAIN FILES IF LOADING RESIDENT SAMPLES` 0x55CC ·
`ERROR - Number of SFX exceeds maximum` 0x5600.

File offset for any of these = VA + 0x90. There are **no other path or filename strings in the
image**; the 37 `.VGM`/`.BD` paths above are the complete set.

`"Invalid sound effect id: %i channel: %i  Call ignored"` is the exact message a silent ported
enemy produces: `PlayResidentSFX` prints it when `GetSpotFXID` fails to find the EffectID.

## 4. Bank format (MEASURED from `ProcessSoundDataHeader` @0x4460)

A `.BD` file is a directory header followed by one contiguous PS-ADPCM sample block.

```
u32  nSFX                                  ; must be 1..299, else "ERROR - Number of SFX exceeds maximum"
struct { u32 w0; u32 w1; u32 w2; } entry[nSFX];   ; 12 bytes each
--- end of header (header length = 4 + 12*nSFX) ---
u8   samples[...]                          ; raw PS-ADPCM, streamed straight to SPU RAM
```

Field decode, straight off the stores at 0x44B8–0x44EC:

| Field | Meaning |
|---|---|
| `w0 >> 16` | **EffectID** (16-bit, global) → runtime `+4` |
| `w0 & 0xFF` | volume percent 0..100 → runtime `+0` |
| `(w0 >> 8) & 0xFF` | flag byte → runtime `+1` |
| `w1` | byte offset of this sample inside the bank's SPU block; runtime `+8` is set to `w1 + bankSpuBase` |
| `w2 >> 16` | pitch / sample-rate word passed to `sceSdSetPitch` → runtime `+0xC` |
| `w2 & 0xFFFF` | second u16 → runtime `+0xE`; loaded and compared to `+0xC` in `PlayResidentSFX`, but the branch goes to the next instruction, so **unused at runtime** (likely a loop point in the authoring tool) |

The parsed directory lands in `ADPCM_SpotFX` @0x7064 (bss, 4,816 bytes = **301 entries × 16
bytes**), and the entry after the last is terminated with `-1` at `+4`.

### EffectID → sample mapping

`GetSpotFXID(id)` @0x2E14 is a **linear search over `ADPCM_SpotFX[0..299]` comparing the 16-bit
`+4` field**, stopping at the `-1` terminator, returning the index or −1. There is no hashing, no
implicit ordering, no id-range assumption. **EffectIDs are an explicit per-entry field, so any set
of ids in any order is legal.** This is the single most important authorability fact.

`PlayResidentSFX(id, chan, volL, volR, x)` @0x2FA0 then:
`idx = GetSpotFXID(id)`; on −1 prints "Invalid sound effect id"; otherwise scales volL/volR by
`entry.volume/100`, records it in `ChannelBaseVolume[chan]`, and calls
`SetVoiceData(chan, entry.pitch, volL', volR', entry.spuAddr, x)` @0x1188, which issues
`sceSdSetParam` (ADSR) ×2 and `sceSdSetAddr(chan|0x2040, spuAddr)`, then key-on via
`sceSdSetSwitch`. Channels 0–23 map to core 0 (`sceSdSetSwitch 0x1500`), 24+ to core 1 (`0x1501`).

### Upload to SPU RAM

The resident path reuses the CD streaming engine, not a one-shot DMA:

1. `USR_StartStream(..., 127, ...)` sets the resident flag and arms the request.
2. `StartStream` @0x4B8: for the resident case, `SpuWriteCursor (*0x5F44) = StreamMemoryEnd`;
   for music it is instead the music buffer's SPU base.
3. The first CD chunk lands in the IOP buffer; `cdend1` @0x1044 calls `ProcessSoundDataHeader`,
   which parses the directory, **advances the IOP read pointer past the header**, and records the
   header length at `StreamBuffer+0x9C`. `cdend1` then subtracts that length from the byte count,
   so **the header itself is never sent to the SPU** — `w1` offsets are relative to the start of
   the sample block.
4. `UpdateStream` / `StreamADPCM` push the sample block to SPU RAM with **`sceSdVoiceTrans`**
   (six call sites: 0x17F8, 0x1DDC, 0x1E1C, 0x1E84, 0x1F14, 0x20D0, 0x2344).
5. On completion, `StreamADPCM` @0x1B1C–0x1B30 does
   **`StreamMemoryEnd += (ADPCM_FileInfo[i].length + 63) & ~63`** — the SPU cursor advances by
   the whole file size, rounded up to 64 bytes.

Sample encoding is PS-ADPCM (`libsd` + `sceSdVoiceTrans` + the `sce_adpcm_*` naming); the IRX
never touches sample bytes, it only DMAs them, so there is no encoder/decoder in the image to
cross-check against. That the samples are PS-ADPCM/VAG-body data is **INFERRED** from the
transport, not measured from a parser.

## 5. The budget (MEASURED where stated)

- **SPU2 RAM is 2 MB and the driver knows it.** `SOUND_EnableEffects` @0x3ec4 places the reverb
  work area at **`0x001FFFFF`** for the first core and **`0x001DFFFF`** for the second (0x3F0C,
  0x3F4C). So reverb eats the top 128 KB per active core, from the top down.
- **The bank's SPU base is chosen by the EE, not the IRX.** RPC command 14 sets
  `StreamMemoryEnd = (args[0]<<16)|args[1]`. The IRX has no hardcoded base and, critically,
  **no ceiling check at all** — nothing compares `StreamMemoryEnd` against 0x200000 or against
  the reverb area. Overflowing SPU RAM would corrupt silently.
- **One bank occupies `roundup64(filesize)` bytes of SPU RAM**, header included (the header is
  not transferred but the cursor still advances by the full file length, leaving
  `4 + 12*nSFX` bytes of slack at the end of each bank).
- **Banks stack in SPU RAM, but the id table does not.** `StreamMemoryEnd` advances after each
  load, so two banks *can* physically coexist. However `ProcessSoundDataHeader` always refills
  `ADPCM_SpotFX` **from index 0** and re-terminates at `nSFX`. Loading a second bank therefore
  destroys the first bank's id→sample directory while its samples sit orphaned in SPU RAM. This
  is the driver-side reason the "load two banks" approach cannot work, and it is measured, not
  assumed. (The EE-side single-resident-id cache at `PSound+0x9F0` from prior work is a second,
  independent instance of the same restriction.)
- **Hard limits the IRX does enforce:** `nSFX < 300` (checked, with its own error string);
  `ADPCM_SpotFX` has room for 301 entries; `SpotFX_LoadAndTransfer` rejects its slot argument
  `>= 300`; `ADPCM_FileNames` is walked for at most 100 entries and `ADPCM_FileInfo` holds 101,
  so **new `.BD` ids cannot be added without patching the IRX** — but ids 208–220 already exist.
- **Whether bank 208 leaves headroom is NOT knowable from the IRX.** The IRX contains no bank
  sizes; sizes are discovered at runtime by `lseek(fd, 0, SEEK_END)` in `InitADPCMFiles`. The
  same is true of the individual sample sizes the foreign enemies need — those live in the `.BD`
  files. **`game_files/` contains no `.BD` files at all** (verified: only `.PSX`, `.BEF`, `.PRS`,
  `.PRT`, `.TEX`, `.ELF`, `.IRX`, and two EE executables).

## 6. Feasibility verdict

**NEEDS MORE FILES — and once they are supplied, the mechanism for a merged bank exists.**

Nothing in the driver blocks authoring a merged bank. The pieces that make it possible are all
measured and named:

- the id→sample mapping is an **explicit 16-bit EffectID field per entry**, resolved by linear
  search, so foreign enemies' ids (0x0D's 430–434, 0x16's 404–406, and the rest) can simply be
  appended as extra entries;
- the header is a trivial `u32 count` + `12×count` array with byte offsets relative to the sample
  block, so it can be rebuilt mechanically;
- the driver imposes no size ceiling on a bank, and no per-file layout it can reject beyond
  `nSFX < 300` — GRAVE's own count plus a handful of imports is nowhere near that;
- the merged file can keep the name `GRAVE.BD` and load as bank 208 with no EE or IRX patch,
  because the id→path table is static and already points at that name.

The two real constraints to respect are: **`nSFX` must stay below 300**, and the total
`roundup64(filesize)` must still fit under the reverb work area at 0x1FFFFF minus whatever the EE
set `StreamMemoryEnd` to via command 14 — the driver will not tell you when you exceed it.

### Files the user must extract from the ISO before this can go further

Exact ISO paths, from the table in section 3:

- `\PSXDATA\SOUND\GRAVE.BD` — the merge target (bank 208)
- `\PSXDATA\SOUND\SWAMP.BD` — source for Crocodile (0x0D) and Axe_Guard (0x16) samples
- `\PSXDATA\SOUND\ICE.BD` — source for Snowman (0x1D)
- `\PSXDATA\SOUND\CASTLE.BD` — source for Hammer_Devil (0x27)
- `\PSXDATA\SOUND\UNDER.BD` — for completeness, the fifth world

With those four to five files in `game_files/`, the next research step is purely mechanical:
parse each header with the layout in section 4, confirm it round-trips, and measure the actual
byte cost of the specific samples behind EffectIDs 404–406, 430–434, 345/499/856, 364/368–370 and
1367. Until then the byte budget is unmeasurable and no feasibility number can be quoted.

### What is INFERRED rather than measured

- Sample encoding is PS-ADPCM/VAG-body. Inferred from `libsd` + `sceSdVoiceTrans`; the IRX has
  no sample parser to confirm it.
- `w2 & 0xFFFF` being a loop point. Its runtime use is dead code; the interpretation is a guess.
- The EE's actual command-14 value (the SPU base for bank 208) and therefore the true headroom
  figure. That lives in the EE executable, which this pass did not open.
