"""Cross-world enemies for the debug menu's spawn tool.

The debug menu can spawn any enemy whose class the current world's files hold. Out of the box that is only the world's
own enemies: a Dark Knight lives in CASTLE.BEF / .PRS / .PRT and nowhere else. This tool does what the randomizer's
cross-world mode does (maximo1randomizer/randomizer/cross_world.py), with the randomizer's own code and its own gates,
but without randomizing anything: for each of the five worlds, every enemy on the randomizer's cross-world allowlist
for that world is ported into it -- its class entry (and the companions it spawns, like the Bomb Skeleton's bomb) into
the world's .BEF, its meshes and animations into the .PRS, its textures into the .PRT -- and the world's sound bank
gets their samples. Then the debug menu lists the ported enemies in that world too.

Sound: on a PS2 the merged banks do not fit (2 MB of SPU RAM, and the sound driver MJOLNIR.IRX takes fewer than 300
effects per bank), so the randomizer leaves most ported enemies silent. With --sound-ram-mb 16 (what the launcher
passes when maximo_boot.exe has PS2X_SPU2_RAM_MB, and it then starts the game with PS2X_SPU2_RAM_MB=16) both limits go:
the emulated SPU2 gets 16 MB, and the driver on the disc is patched to hold 1000 effects per bank (its effect table
moves to new memory at the end of the module; see patch_sound_table). Effects whose id the enemy's script computes at
run time cannot be found by the randomizer's static scan and stay silent either way.

Differences from a randomizer run:
  * No seeded draw and no budget walk. The randomizer picks a few routes per world because the PS2's 6.25 MB resource
    pool holds a world's .PRS + .PRT; with the launcher's Expanded memory (MAXIMO_RAM=more, which the launcher turns on
    with this option) that pool is four times larger, and every route of every world fits (the largest, GRAVE, comes to
    about 10.6 MB). The ceiling below is that larger pool, at the randomizer's 90.4% occupancy.
  * No level is changed: nothing is placed. The enemies only exist for the spawn tool.
  * A root enemy that an earlier route already brought along as a companion is not injected a second time (the
    randomizer never draws both in one world, so it does not guard against it; ICE fails verify_session otherwise).
  * The game files are the ones in .\\disc (any region; checked by the randomizer's own structural gates, and every
    world must pass verify_session or it is left untouched).

The originals are saved once under dist\\disc_world_original and every run starts from them, so running it again (or
after a randomizer update) gives the same result. --restore puts them back.

    python tools\\cross_world_enemies.py                 (dist\\disc, maximo1randomizer next to tools)
    python tools\\cross_world_enemies.py --restore
    python tools\\cross_world_enemies.py --disc D:\\x\\disc --randomizer D:\\y\\maximo1randomizer
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import struct
import sys
import tempfile
import time
import types
from pathlib import Path
from types import SimpleNamespace

# world -> (folder under PSXDATA, archive stem)
WORLDS = {
    "grave": ("GRAVE", "GRAVE"),
    "castle": ("CASTLE", "CASTLE"),
    "swamp": ("SWAMP", "SWAMP"),
    "ice": ("ICESHIP", "ICE"),
    "under": ("UNDER", "UNDER"),
}
# The PAL disc has one sound folder per language; the world banks are the same in each (checked before writing).
SOUND_DIRS = ("SOUND", "FSOUND", "GSOUND", "ISOUND", "SSOUND")
RESOURCE_POOL_EXPANDED = 0x640000 * 4  # the +0x260 pool with MAXIMO_RAM=more (kMidPoolScale in maximo_boot.cpp)
OCCUPANCY = 0.904                      # the randomizer's measured safe occupancy (roster_selection.LOADED_MAX_PCT)
STAMP = "crossworld_applied.txt"
VERSION = "13"  # 13: arenas also get the ghost and the wooden chest (survival); 12: arenas keep their own sound bank, every bank holds every sound, common enemies bring their companions
DRIVER = Path("PSXIRX") / "MJOLNIR.IRX"
SOUND_ENTRIES = 1000  # effects per bank with the patched driver (each world needs about 340)


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def disc_files() -> list[Path]:
    """Every file this tool may change, relative to the disc folder (the backup has the same layout)."""
    files = []
    for folder, stem in WORLDS.values():
        for ext in ("BEF", "PRS", "PRT"):
            files.append(Path("PSXDATA") / folder / f"{stem}.{ext}")
        for sound in SOUND_DIRS:
            files.append(Path("PSXDATA") / sound / f"{stem}.BD")
    for _label, _world, folder, stem, bank, _second in EXTRA_TARGETS:
        for ext in ("BEF", "PRS", "PRT"):
            files.append(Path("PSXDATA") / folder / f"{stem}.{ext}")
        if bank:
            for sound in SOUND_DIRS:
                files.append(Path("PSXDATA") / sound / f"{bank}.BD")
    files.append(DRIVER)
    return files


def save_originals(disc: Path, backup: Path) -> None:
    for rel in disc_files():
        src, dst = disc / rel, backup / rel
        if src.is_file() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def write(path: Path, data: bytes) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(data)
    temp.replace(path)


def restore(disc: Path, backup: Path) -> int:
    n = 0
    for rel in disc_files():
        src = backup / rel
        dst = disc / rel
        if src.is_file() and (not dst.is_file() or md5(dst) != md5(src)):
            write(dst, src.read_bytes())
            n += 1
    (backup / STAMP).unlink(missing_ok=True)
    return n


class IrxPatchError(Exception):
    pass


def patch_sound_table(data: bytes, entries: int) -> tuple[bytes, str]:
    """MJOLNIR.IRX with room for `entries` sound effects per bank instead of 299.

    The driver keeps a bank's directory in ADPCM_SpotFX (bss, 301 entries of 16 bytes) and refuses banks of 300 or
    more (ProcessSoundDataHeader) and stops looking at 300 (GetSpotFXID). The table moves to new bss at the end of
    the module (the program header's memsz grows; the loader zeroes it), every HI16/LO16 pair that builds its
    address is rewritten, and both limits become `entries`. Everything is found through the module's own symbol
    table and relocations, so any region's driver works; anything unexpected refuses the patch.
    """
    d = bytearray(data)
    if d[:4] != b"\x7fELF" or d[4] != 1 or d[5] != 1:
        raise IrxPatchError("not a 32-bit little-endian ELF")
    e_phoff, e_shoff = struct.unpack_from("<II", d, 0x1C)
    e_phentsize, e_phnum, e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHHHH", d, 0x2A)
    secs = [struct.unpack_from("<IIIIIIIIII", d, e_shoff + i * e_shentsize) for i in range(e_shnum)]
    shstr = secs[e_shstrndx]

    def name(off, strtab):
        s = strtab[4] + off
        return bytes(d[s:d.index(b"\0", s)]).decode("latin1")

    by_name = {name(s[0], shstr): s for s in secs}
    for need in (".text", ".rel.text", ".symtab", ".strtab"):
        if need not in by_name:
            raise IrxPatchError(f"no {need} section")
    text, reltext, symtab, strtab = (by_name[n] for n in (".text", ".rel.text", ".symtab", ".strtab"))
    tbase = text[4]  # file offset of .text (vaddr 0)
    syms = {}
    for i in range(symtab[5] // 16):
        st_name, st_value, st_size, st_info, st_other, st_shndx = struct.unpack_from("<IIIBBH", d, symtab[4] + i * 16)
        if st_name:
            syms[name(st_name, strtab)] = (st_value, st_size)
    for need in ("ADPCM_SpotFX", "GetSpotFXID", "ProcessSoundDataHeader"):
        if need not in syms:
            raise IrxPatchError(f"no {need} symbol")
    table, table_size = syms["ADPCM_SpotFX"]
    if table_size != 301 * 16:
        raise IrxPatchError(f"ADPCM_SpotFX is {table_size} bytes, not 301 entries")

    # The one PT_LOAD: its memsz grows by the new table.
    load = None
    for i in range(e_phnum):
        p_type, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz = struct.unpack_from("<IIIIII", d, e_phoff + i * e_phentsize)
        if p_type == 1:
            if load is not None:
                raise IrxPatchError("more than one PT_LOAD")
            load = (e_phoff + i * e_phentsize, p_vaddr, p_memsz)
    if load is None or load[1] != 0:
        raise IrxPatchError("no PT_LOAD at address 0")
    new_table = (load[2] + 15) & ~15
    new_memsz = new_table + (entries + 1) * 16

    def word(va):
        return struct.unpack_from("<I", d, tbase + va)[0]

    def put(va, w):
        struct.pack_into("<I", d, tbase + va, w)

    # HI16/LO16 pairs (section-relative, symbol 0): an HI16 pairs with the LO16s that follow it.
    pairs, hi = [], None
    for i in range(reltext[5] // 8):
        off, info = struct.unpack_from("<II", d, reltext[4] + i * 8)
        t = info & 0xFF
        if t == 5:
            hi = off
        elif t == 6 and hi is not None:
            lo16 = word(off) & 0xFFFF
            addr = ((word(hi) & 0xFFFF) << 16) + (lo16 - 0x10000 if lo16 & 0x8000 else lo16)
            pairs.append((hi, off, addr))
    hits = [(h, l, a) for h, l, a in pairs if table <= a < table + table_size]
    if not hits:
        raise IrxPatchError("no reference to ADPCM_SpotFX")
    for h, l, a in pairs:  # an HI16 shared with something that is not the table would move that too
        if any(h == hh for hh, _, _ in hits) and not table <= a < table + table_size:
            raise IrxPatchError(f"the address half at 0x{h:X} is shared with 0x{a:X}")
    for h, l, a in hits:
        new = new_table + (a - table)
        put(h, (word(h) & 0xFFFF0000) | (((new + 0x8000) >> 16) & 0xFFFF))
        put(l, (word(l) & 0xFFFF0000) | (new & 0xFFFF))

    # The two limits: `slti rt, rs, 300` inside GetSpotFXID and ProcessSoundDataHeader.
    for fn in ("GetSpotFXID", "ProcessSoundDataHeader"):
        start, size = syms[fn]
        found = [va for va in range(start, start + size, 4)
                 if word(va) >> 26 == 0x0A and word(va) & 0xFFFF == 300]
        if len(found) != 1:
            raise IrxPatchError(f"{fn}: {len(found)} 'below 300' checks, expected 1")
        put(found[0], (word(found[0]) & 0xFFFF0000) | entries)

    struct.pack_into("<I", d, load[0] + 20, new_memsz)
    return bytes(d), (f"sound table moved from 0x{table:X} to 0x{new_table:X} for {entries} entries "
                      f"({len(hits)} references), module memory 0x{load[2]:X} -> 0x{new_memsz:X}")




# ---- bosses -------------------------------------------------------------------------------------------------------
# A boss's class lives only in its arena's archive (GRAVE_B, SWAMP_B, ICE_B, UNDER_B, CASTLE_K, CASTLE_Q). The randomizer
# ports the GraveDigger into GRAVE with randomizer/boss_inject.py: derive_merge_set (the boss plus everything its script
# spawns, transitively), port_boss_assets (every member's meshes, animations and textures, every reference rewritten
# and identity-checked) and merge_bef. All of that is derived from the boss's own data; only three pieces are his
# alone, and load_boss_module loads the module a second time with those generalized: the sanity check that the boss
# is mesh 0 with anims 0..21, the PS2's resource-pool budget, and his script edits at measured offsets. Instead of
# those edits every ported member gets neutralize(): each GotoNextLevel (api 0x71) and CameraSetBossFlag (0xE8) call in
# the measured push/popapiparam/callapi form becomes five nops, so killing a spawned boss neither ends the level nor
# leaves the camera in boss mode. port_boss feeds the module the arena and the world under the file names it opens.
BOSSES = (  # type, name, arena archive, its sound bank
    (0x29, "GraveDigger", "GRAVE_B", "BS_GRAVE"),
    (0x2E, "BokorLaBas", "SWAMP_B", "BS_SWAMP"),
    (0x82, "Captain Cadaver", "ICE_B", "BS_ICE"),
    (0x8E, "Lord Glutterscum", "UNDER_B", "BS_UNDER"),
    (0x9C, "King", "CASTLE_K", "BS_KING"),
    (0x9D, "Queen", "CASTLE_Q", "BS_QUEEN"),
)
# Classes a boss needs that its script finds instead of spawning, so derive_merge_set cannot see them: the Queen
# collects her eight QueenWaypoints with FindApparitions(0x8A); the debug menu lays a ring of them out with her.
BOSS_EXTRA_MEMBERS = {0x9D: (0x8A,), 0xF7: (0xF8, 0xF9, 0xFA)}

# ---- NPCs ---------------------------------------------------------------------------------------------------------
# The sorceresses: class 0x99 in each of the four _B arenas, the same script with that arena's model; the game creates
# her when the boss dies. Ported like a boss, one arena at a time, and each renamed to a type of its own right after
# (the classes would collide on 0x99 otherwise). Her GameVarAdd calls (the rescued-sorceress count, GameVar 12) are
# removed like the exit calls, so spawning one does not change the save's progress.
# Grim: on the continue screen he is mesh 0 of GAME/CONTINUE.PRS, drawn by the game's own code, with no class at all.
# He gets one here: ICE_B's Sea_Weed class (one mesh, one animation, a script that loops it) with its references
# pointed at Grim's mesh and at one of his four animations (continue-screen records 0, 1, 2 and 5: one class each),
# in a class table of their own beside CONTINUE.PRS/.PRT, which the boss porter then takes as an arena.

GRIM_TYPES = (0xF7, 0xF8, 0xF9, 0xFA)          # Grim with continue-screen animation 0, 1, 2, 5
GRIM_ANIMS = (0, 1, 2, 5)
SORCERESS_TYPES = {"GRAVE_B": 0xFB, "SWAMP_B": 0xFC, "ICE_B": 0xFD, "UNDER_B": 0xFE}
TEMPLATE = ("ICE_B", 0x12)                     # Sea_Weed: one mesh, one animation, a script that only loops it

def set_type(blob, tid):
    b = bytearray(blob)
    struct.pack_into("<I", b, 0x10, tid)
    return bytes(b)

def make_grim_bef(_bef, continue_bef, template_blob):
    entries = []
    for tid, anim in zip(GRIM_TYPES, GRIM_ANIMS):
        blob = set_type(template_blob, tid)
        for ref in _bef.parse_material_refs(blob):
            if ref.is_global:
                continue
            if ref.category == 1:
                blob = _bef.set_material_ref_index(blob, ref, 0)       # CONTINUE.PRS mesh 0: Grim
            elif ref.category == 2:
                blob = _bef.set_material_ref_index(blob, ref, anim)
        entries.append(_bef.BefEntry(type_id=tid, data_offset=0, data=blob))
    return _bef.BefFile.parse(continue_bef).rebuild_with_injections(entries)

def retype(bef_bytes, old, new):
    """The class `old` becomes `new` in a .BEF: its table-of-contents entry and the type field of its data."""
    b = bytearray(bef_bytes)
    n = struct.unpack_from("<I", b, 0x14)[0]
    hits = 0
    for i in range(n):
        off = 0x18 + i * 8
        tid, data = struct.unpack_from("<II", b, off)
        if tid == old:
            if struct.unpack_from("<I", b, data + 0x10)[0] != old:
                raise ValueError(f"class 0x{old:02X}'s data does not carry its type")
            struct.pack_into("<I", b, off, new)
            struct.pack_into("<I", b, data + 0x10, new)
            hits += 1
    if hits != 1:
        raise ValueError(f"class 0x{old:02X} found {hits} times")
    return bytes(b)

ARENA_FOLDER = {"GRAVE_B": "GRAVE", "SWAMP_B": "SWAMP", "ICE_B": "ICESHIP", "UNDER_B": "UNDER",
                "CASTLE_K": "CASTLE", "CASTLE_Q": "CASTLE"}

# ---- extra targets: the boss arenas and the menu level ----------------------------------------------------------
# So the debug menu can spawn everything there too. Each is opened like a world (WorldPortSession with the archive's
# stem, the path randomizer.boss_arena uses for arenas). The permission oracle only lists routes INTO a world from
# OTHER worlds, so an arena gets two passes: the routes into its own world (everything foreign to it), then the routes
# into a second world restricted to enemies native to the first (the arena's own world's enemies, which its archive
# lacks).
# (label, world for the first pass, folder, archive stem, sound bank or None, world for the natives pass)
EXTRA_TARGETS = (
    ("grave arena", "grave", "GRAVE", "GRAVE_B", "BS_GRAVE", "castle"),
    ("swamp arena", "swamp", "SWAMP", "SWAMP_B", "BS_SWAMP", "castle"),
    ("ice arena", "ice", "ICESHIP", "ICE_B", "BS_ICE", "castle"),
    ("under arena", "under", "UNDER", "UNDER_B", "BS_UNDER", "castle"),
    ("king arena", "castle", "CASTLE", "CASTLE_K", "BS_KING", "grave"),
    ("queen arena", "castle", "CASTLE", "CASTLE_Q", "BS_QUEEN", "grave"),
)
# The menu level (GAME/SELECT) is left out: porting into it ran out of memory in testing (killed at 8 GB).


def load_boss_module(randomizer, ceiling):
    """randomizer.boss_inject, loaded once more with its three GraveDigger-only pieces generalized."""
    path = Path(randomizer.__file__).parent / "boss_inject.py"
    src = path.read_text(encoding="utf-8")
    def rep(a, b):
        nonlocal src
        if src.count(a) != 1:
            raise RuntimeError(f"boss_inject.py changed: {a[:60]!r} found {src.count(a)} times")
        src = src.replace(a, b)
    rep("    if boss_mesh_src != [BOSS_SOURCE_MESH_INDEX]:", "    if False:  # generalized: any boss")
    rep("    if boss_anim_src != list(BOSS_SOURCE_ANIM_INDICES):", "    if False:  # generalized: any boss")
    rep("    budget = _roster_selection.pool_budget(raised=POOL_BUDGET_RAISED)", "    budget = _ANY_BOSS_BUDGET")
    mod = types.ModuleType("randomizer._boss_any")
    mod.__package__ = "randomizer"
    mod.__file__ = str(path)
    mod._ANY_BOSS_BUDGET = ceiling
    sys.modules[mod.__name__] = mod
    exec(compile(src, str(path), "exec"), mod.__dict__)
    # The GraveDigger's measured script spans are his alone; every boss gets the generic pass below instead.
    mod.NEUTRALIZE_LEVEL_EXIT = False
    mod.REMOVE_BOSS_CAMERA_FLAG = False
    mod.STOP_CUTSCENE_ON_DEATH = False
    mod.REDIRECT_LEVEL_EXIT = False
    mod.KILL_INSTEAD_OF_DEATH_SEQUENCE = False
    mod.REPLACE_EXIT_WITH_UNFREEZE = False
    return mod

EXIT_APIS = (0x71, 0xE8)  # GotoNextLevel, CameraSetBossFlag
# Basic Skeleton, Sword Skeleton, Guard Skeleton, Axe Skeleton, Basic Zombie, Torso Zombie (what a Basic Zombie leaves)
COMMON_ENEMIES = (0x47, 0x39, 0x54, 0x2F, 0x48, 0x6A)
# Also into every arena, for survival: the Ghost and the Wooden Chest (with what each spawns: the chest's mimic,
# wizard and prizes).
ARENA_EXTRAS = (0x6B, 0x25)

def neutralize(blob, _bs, game_vars=False):
    """Every `push imm8|imm32; popapiparam; callapi <api>` for the level exit and the boss camera flag -> 5 or 8 nops
    (the GraveDigger, Captain Cadaver and the Queen push a byte; BokorLaBas, Lord Glutterscum and the King push their
    exit delay as a 32-bit c_push, which the byte-only match used to miss, so those bosses still ended the level); with
    game_vars, every `push imm8; push imm8; popsetapiparam 2; callapi GameVarAdd` -> 8 nops too."""
    blob = bytes(blob)
    try:
        section = _bs.script_section(blob)
    except ValueError:
        return blob, 0
    dis = _bs.disassemble(section, _bs.REVISION_RETAIL)
    if not dis.clean:
        return blob, 0
    start = int.from_bytes(blob[_bs.BLOB_SCRIPT_OFFSET_OFFSET:_bs.BLOB_SCRIPT_OFFSET_OFFSET + 4], "little")
    ins = dis.instructions
    out = bytearray(blob)
    n = n8 = added = 0
    for i in range(2, len(ins)):
        call, pop, push = ins[i], ins[i - 1], ins[i - 2]
        if (len(call.raw) == 2 and call.raw[0] == 0x8C and call.raw[1] in EXIT_APIS and pop.raw == b"\x15"
                and ((len(push.raw) == 2 and push.raw[0] == 0x04) or (len(push.raw) == 5 and push.raw[0] == 0x05))
                and push.offset + len(push.raw) + 1 == call.offset):
            span = len(push.raw) + 3
            out[start + push.offset:start + push.offset + span] = b"\xFE" * span
            added += span - 3
            n += 1
        elif (game_vars and i >= 3 and call.raw == b"\x8C\x69" and pop.raw == b"\x17\x02"
              and len(push.raw) == 2 and push.raw[0] == 0x04 and len(ins[i - 3].raw) == 2 and ins[i - 3].raw[0] == 0x04
              and ins[i - 3].offset + 6 == call.offset):
            out[start + ins[i - 3].offset:start + ins[i - 3].offset + 8] = b"\xFE" * 8
            n8 += 1
    if n or n8:
        after = _bs.disassemble(_bs.script_section(bytes(out)), _bs.REVISION_RETAIL)
        if not after.clean or len(after.instructions) != len(ins) + added + 4 * n8:  # 3 -> 5/8 and 4 -> 8 nops
            return blob, 0
        n += n8
    return bytes(out), n

# The Queen's lighting: her script starts with EnableWorldDynamicLights, UseStaticLights(0) and SetAmbientLightColor
# (0.01, 0.01, 0.01) - her arena goes dark - and makes a direction light it keeps re-colouring (SetLightColour,
# SetLightDirection: the flicker). Spawned anywhere else that turned the screen black and flickering, so in her ported
# copy each of those calls goes with exactly the arguments it pushes (pure pushes only; any other form is left alone).
LIGHT_APIS = (0xF5, 0xF3, 0xF2, 0x79, 0x80)
PUSH_WORDS = {0x04: 1, 0x05: 1, 0x06: 1, 0x08: 1, 0x18: 3, 0x19: 3}  # by_c_push, c_push, by_l_push, by_m_push, *pushset

def neutralize_lights(blob, _bs):
    blob = bytes(blob)
    try:
        section = _bs.script_section(blob)
    except ValueError:
        return blob, 0
    dis = _bs.disassemble(section, _bs.REVISION_RETAIL)
    if not dis.clean:
        return blob, 0
    start = int.from_bytes(blob[_bs.BLOB_SCRIPT_OFFSET_OFFSET:_bs.BLOB_SCRIPT_OFFSET_OFFSET + 4], "little")
    ins = dis.instructions
    out = bytearray(blob)
    n = added = 0
    for i in range(1, len(ins)):
        call, pop = ins[i], ins[i - 1]
        if not (len(call.raw) == 2 and call.raw[0] == 0x8C and call.raw[1] in LIGHT_APIS):
            continue
        if pop.raw == b"\x15":
            need = 1
        elif len(pop.raw) == 2 and pop.raw[0] == 0x17:
            need = pop.raw[1]
        else:
            continue
        j, got = i - 2, 0
        while j >= 0 and got < need and ins[j].raw[0] in PUSH_WORDS:
            got += PUSH_WORDS[ins[j].raw[0]]
            j -= 1
        if got != need:
            continue
        first = ins[j + 1]
        span = call.offset + 2 - first.offset
        out[start + first.offset:start + first.offset + span] = b"\xFE" * span
        added += span - (i - j)
        n += 1
    if n:
        after = _bs.disassemble(_bs.script_section(bytes(out)), _bs.REVISION_RETAIL)
        if not after.clean or len(after.instructions) != len(ins) + added:
            return blob, 0
    return bytes(out), n

# SetMeshEffect(mesh, anim) takes its two resources as script constants, category << 24 | index into the archive's
# own tables (the sorceress's appearance effect: three such pairs; the GraveDigger: one). The class's material
# section does not list them, so port_boss_assets does not carry them and the constants would name whatever sits at
# those indices in the target world (Sorceress 1 and 2 then never show up). Each pair is ported through a helper
# class -- the Grim template with its references pointed at that mesh and anim, given a free type id and merged as
# one more member -- and the constants are rewritten to where the helper's references landed.
MESH_EFFECT_API = 0xCC
HELPER_TYPES = (0x90, 0x91, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0xB2, 0xB3, 0xB4, 0xB6, 0xB7, 0xB8, 0xC1, 0xC2,
                0xC3, 0xC7, 0xCA, 0xD5, 0xD6)  # used by no class table on the disc


def mesh_effect_pairs(blob, _bs):
    """[(script offset of the mesh push, mesh index, script offset of the anim push, anim index)] for every
    `c_push mesh; c_push anim; popsetapiparam 2; callapi SetMeshEffect` with literal resources."""
    try:
        dis = _bs.disassemble(_bs.script_section(bytes(blob)), _bs.REVISION_RETAIL)
    except ValueError:
        return []
    ins, out = dis.instructions, []
    for i in range(3, len(ins)):
        if ins[i].raw != bytes((0x8C, MESH_EFFECT_API)) or ins[i - 1].raw != b"\x17\x02":
            continue
        a, b = ins[i - 3], ins[i - 2]
        if len(a.raw) != 5 or a.raw[0] != 0x05 or len(b.raw) != 5 or b.raw[0] != 0x05:
            continue
        va, vb = int.from_bytes(a.raw[1:], "little"), int.from_bytes(b.raw[1:], "little")
        found = {va >> 24: (a.offset, va & 0xFFFFFF), vb >> 24: (b.offset, vb & 0xFFFFFF)}
        if set(found) == {1, 2}:
            out.append((found[1][0], found[1][1], found[2][0], found[2][1]))
    return out


def port_boss(bi, _bef, _bs, *, boss_type, arena_dir_files, target_files, log, rename=None, game_vars=False,
              template=None):
    """Port one boss (and everything it spawns) into the target world trio. arena_dir_files / target_files:
    dicts ext -> Path. Returns (ok, message, {ext: bytes}, member_ids)."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        for ext in ("BEF", "PRS", "PRT"):
            shutil.copy2(arena_dir_files[ext], t / f"GRAVE_B.{ext}")
            shutil.copy2(target_files[ext], t / f"GRAVE.{ext}")
        bi.BOSS_TYPE = boss_type
        boss_bef = _bef.BefFile.parse(t / "GRAVE_B.BEF")
        host_bef = _bef.BefFile.parse(t / "GRAVE.BEF")
        if boss_bef.get_entry(boss_type) is None:
            return False, "not in its arena's class table", None, ()
        if host_bef.get_entry(rename or boss_type) is not None:
            return True, "already there", None, ()
        closure = bi.derive_merge_set(boss_bef, host_bef)
        if not closure.ok:
            return False, f"{closure.reason}: {'; '.join(closure.details)[:200]}", None, ()
        have = {m.type_id for m in closure.members}
        extra = tuple(bi.ClosureMember(type_id=t, depth=1, parent=boss_type, direct=True)
                      for t in BOSS_EXTRA_MEMBERS.get(boss_type, ()) if t not in have and boss_bef.get_entry(t) is not None)
        # SetMeshEffect resources: a helper class per (mesh, anim) pair, added to the arena and to the members.
        host_ids = host_bef.type_ids()
        effect_pairs = {}  # member type -> its pairs
        helper_of = {}     # (mesh, anim) -> helper type
        if template is not None:
            free = [h for h in HELPER_TYPES if h not in host_ids and boss_bef.get_entry(h) is None]
            helpers = []
            for m in closure.members + extra:
                if m.type_id in host_ids or boss_bef.get_entry(m.type_id) is None:
                    continue
                pairs = mesh_effect_pairs(boss_bef.get_entry(m.type_id).data, _bs)
                if pairs:
                    effect_pairs[m.type_id] = pairs
                for _mo, mesh, _ao, anim in pairs:
                    if (mesh, anim) in helper_of:
                        continue
                    if not free:
                        return False, "no free type id left for a SetMeshEffect helper", None, ()
                    hid = free.pop(0)
                    blob = set_type(template, hid)
                    for ref in _bef.parse_material_refs(blob):
                        if not ref.is_global and ref.category in (1, 2):
                            blob = _bef.set_material_ref_index(blob, ref, mesh if ref.category == 1 else anim)
                    helpers.append(_bef.BefEntry(type_id=hid, data_offset=0, data=blob))
                    helper_of[(mesh, anim)] = hid
            if helpers:
                (t / "GRAVE_B.BEF").write_bytes(boss_bef.rebuild_with_injections(helpers))
                boss_bef = _bef.BefFile.parse(t / "GRAVE_B.BEF")
                extra += tuple(bi.ClosureMember(type_id=h.type_id, depth=1, parent=boss_type, direct=True)
                               for h in helpers)
        if extra:
            import dataclasses
            closure = dataclasses.replace(closure, members=tuple(sorted(closure.members + extra, key=lambda m: m.type_id)))
        blobs = {m.type_id: boss_bef.get_entry(m.type_id).data for m in closure.members if m.type_id not in host_ids}
        port = bi.port_boss_assets(t, blobs, log=log)
        if not port.ok:
            return False, f"{port.reason}: {'; '.join(port.details)[:300]}", None, ()
        rewritten = {}
        exits = 0
        for tid, blob in port.blobs:
            rewritten[tid], k = neutralize(blob, _bs, game_vars)
            exits += k
            if boss_type == 0x9D and tid == 0x9D:
                rewritten[tid], lk = neutralize_lights(rewritten[tid], _bs)
                log(f"the Queen: {lk} lighting call(s) removed")
        merged = bi.merge_bef(host_bef, boss_bef, closure, mesh_count=port.mesh_count,
                              anim_count=port.anim_count, blobs=rewritten)
        if not merged.ok:
            return False, f"{merged.reason}: {'; '.join(merged.details)[:300]}", None, ()
        bef_bytes = merged.data
        if effect_pairs:
            bef_bytes = bytearray(bef_bytes)
            merged_bef = _bef.BefFile.parse(bytes(bef_bytes))
            landed = {}
            for (mesh, anim), hid in helper_of.items():
                refs = {r.category: r.index for r in _bef.parse_material_refs(merged_bef.get_entry(hid).data)
                        if not r.is_global and r.category in (1, 2)}
                landed[(mesh, anim)] = (refs[1], refs[2])
            toc = {struct.unpack_from("<I", bef_bytes, 0x18 + i * 8)[0]: struct.unpack_from("<I", bef_bytes, 0x1C + i * 8)[0]
                   for i in range(struct.unpack_from("<I", bef_bytes, 0x14)[0])}
            fixed = 0
            for tid, pairs in effect_pairs.items():
                data = toc[tid]
                blob = merged_bef.get_entry(tid).data
                script = int.from_bytes(blob[_bs.BLOB_SCRIPT_OFFSET_OFFSET:_bs.BLOB_SCRIPT_OFFSET_OFFSET + 4], "little")
                if mesh_effect_pairs(blob, _bs) != pairs:
                    return False, f"class 0x{tid:02X}'s SetMeshEffect calls moved in the merge", None, ()
                for mo, mesh, ao, anim in pairs:
                    new_mesh, new_anim = landed[(mesh, anim)]
                    struct.pack_into("<I", bef_bytes, data + script + mo + 1, 0x01000000 | new_mesh)
                    struct.pack_into("<I", bef_bytes, data + script + ao + 1, 0x02000000 | new_anim)
                    fixed += 1
            bef_bytes = bytes(bef_bytes)
            _bef.BefFile.parse(bef_bytes)
            log(f"SetMeshEffect: {fixed} call(s) pointed at their ported resources ({len(helper_of)} helper class(es))")
        if rename is not None:
            bef_bytes = retype(bef_bytes, boss_type, rename)
            _bef.BefFile.parse(bef_bytes)  # still a valid class table
        return True, (f"{len(rewritten)} class(es), +{len(port.prs) - target_files['PRS'].stat().st_size:,} B models, "
                      f"+{len(port.prt) - target_files['PRT'].stat().st_size:,} B textures, {exits} call(s) removed"
                      + (f", class 0x{boss_type:02X} renamed 0x{rename:02X}" if rename is not None else "")), \
            {"BEF": bef_bytes, "PRS": port.prs, "PRT": port.prt}, tuple(sorted(rewritten))


# ---- texture palettes ----------------------------------------------------------------------------------------------
# The texture manager holds a world's palettes in a table of 175 pointers with nothing after it but its own video-memory
# allocator (func_00194540 fills it with no bounds check), and each texture takes its palette out of the table, so
# palettes can neither be shared nor exceed 175. With the bosses, Grim and the sorceresses, GRAVE and CASTLE need a
# few more; the ports run with a higher count and fit_palettes then turns the smallest ported PSMT8 textures into
# 24-bit ones, the direct-colour form the game's own .PRT files use (format 0x01, 3 bytes a pixel, the expansion to
# 32 bits done by the loader; transparency in the trailing opacity block, flag 2, 0-127). The palette is read the way
# the GS reads a 256-colour CSM1 palette (index bits 3 and 4 swapped: the game's palettes are smooth in that order).
def fit_palettes(prt, limit, protect, tp):
    """A .PRT with at most `limit` palettes: the smallest PSMT8 textures from entry `protect` on (the ported ones)
    become 24-bit textures like the game's own (format 0x01, 3 bytes a pixel, transparency in the trailing opacity
    block, flag 2), and the remaining palettes are renumbered in file order. Returns (bytes, textures converted)."""
    count_a, count_b = struct.unpack_from("<II", prt, 8)
    if count_a <= limit:
        return prt, 0
    pool = [prt[0x20 + i * 1024:0x20 + (i + 1) * 1024] for i in range(count_a)]
    off = 0x20 + count_a * 1024
    entries = []
    for i in range(count_b):
        f = tp._read_header_fields(prt, off)
        if f is None:
            raise ValueError(f"texture {i} at 0x{off:X} does not parse")
        body = prt[off + 16:off + 16 + tp.entry_body_size(f)]
        entries.append([bytearray(prt[off:off + 16]), body, f])
        off += 16 + tp.entry_body_size(f)
    if off != len(prt):
        raise ValueError("the texture walk does not end at the end of the file")
    indexed = [i for i, (_, _, f) in enumerate(entries) if f["fmt"] == tp.FMT_PSMT8]
    candidates = sorted((i for i in indexed if i >= protect),
                        key=lambda i: ((1 << entries[i][2]["log2_w"]) * (1 << entries[i][2]["log2_h"]), i))
    need = count_a - limit
    if len(candidates) < need:
        raise ValueError(f"only {len(candidates)} ported paletted textures for {need} palettes")
    convert = set(candidates[:need])
    swz = [(p & 0xE7) | ((p & 0x08) << 1) | ((p & 0x10) >> 1) for p in range(256)]
    for i in convert:
        raw, body, f = entries[i]
        clut = pool[f["clut_index"]]
        colors = [clut[swz[p] * 4:swz[p] * 4 + 4] for p in range(256)]
        rgb = b"".join(colors[p][:3] for p in body)
        alpha = bytes(min(colors[p][3], 0x7F) if colors[p][3] < 0x80 else 0x7F for p in body)
        opaque = all(a == 0x7F for a in alpha)
        w, h = 1 << f["log2_w"], 1 << f["log2_h"]
        hdr = bytearray(raw)
        hdr[4] = 0x01                                         # PSMCT24
        struct.pack_into("<H", hdr, 6, 0 if opaque else tp.FLAG_OPACITY_MAP)
        struct.pack_into("<I", hdr, 8, w * h * 3)
        hdr[12] = 3                                           # bytes a pixel
        struct.pack_into("<h", hdr, 14, 0)                    # the game's direct-colour entries carry 0
        entries[i] = [hdr, rgb + (b"" if opaque else alpha), None]
    new_pool, n = [], 0
    for i, e in enumerate(entries):
        if e[2] is not None and e[2]["fmt"] == tp.FMT_PSMT8:
            new_pool.append(pool[e[2]["clut_index"]])
            struct.pack_into("<h", e[0], 14, n)
            n += 1
    head = bytearray(prt[:0x20])
    struct.pack_into("<I", head, 8, len(new_pool))
    out = bytes(head) + b"".join(new_pool) + b"".join(bytes(h) + b for h, b, _ in entries)
    # The result walks like a game file: every header parses and the last entry ends the file.
    off = 0x20 + len(new_pool) * 1024
    for i in range(count_b):
        f = tp._read_header_fields(out, off)
        if f is None or (f["fmt"] == tp.FMT_PSMT8) != (i not in convert and i in indexed):
            raise ValueError(f"converted file: texture {i} does not parse as expected")
        off += 16 + tp.entry_body_size(f)
    if off != len(out):
        raise ValueError("converted file: the texture walk does not end at the end of the file")
    return out, len(convert)


def fill_bank(bank_bytes: bytes, sound_dir: Path, sound_bank) -> tuple[bytes, int]:
    """The bank with every effect id of the game's other world and arena banks it lacks, appended (its own effects
    and their order unchanged; the first bank carrying an id gives its sample). Returns (bank, effects added)."""
    order = [stem for _folder, stem in WORLDS.values()] + [b for *_x, b in BOSSES]
    imports = []
    for stem in order:
        path = sound_dir / f"{stem}.BD"
        if not path.is_file():
            continue
        try:
            src = sound_bank.parse(path.read_bytes())
        except sound_bank.SoundBankError:
            continue
        got, _missing = sound_bank.imports_from(src, src.effect_ids, source=stem)
        imports += got
    before = sound_bank.parse(bank_bytes).count
    result = sound_bank.merge(bank_bytes, imports)  # the larger RAM's ceiling and entry limit are set by run()
    return result.data, result.bank.count - before


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--disc", type=Path, default=repo / "dist" / "disc", help="the game's disc folder (has PSXDATA)")
    ap.add_argument("--randomizer", type=Path, default=repo / "maximo1randomizer",
                    help="the randomizer's folder (has the randomizer package)")
    ap.add_argument("--backup", type=Path, default=None, help="where the originals are kept "
                    "(default: disc_world_original next to the disc folder)")
    ap.add_argument("--restore", action="store_true", help="put the original world files back")
    ap.add_argument("--log", type=Path, default=None, help="also write the randomizer's detailed log here")
    ap.add_argument("--clut-table", type=int, default=175,
                    help="texture palettes the ports may use (up to 440); every world is brought back to the game's "
                         "175 after them by turning ported paletted textures into 24-bit ones")
    ap.add_argument("--pool-scale", type=int, default=4, choices=(4, 6),
                    help="the RESOURCE pool's size in retail pools (kMidPoolScale in maximo_boot.cpp)")
    ap.add_argument("--no-arenas", dest="arenas", action="store_false",
                    help="leave the boss arenas alone (only the five worlds)")
    ap.add_argument("--no-bosses", dest="bosses", action="store_false",
                    help="leave the bosses in their arenas (by default all six are added to every world)")
    ap.add_argument("--sound-ram-mb", type=int, default=2, choices=(2, 4, 8, 16, 32),
                    help="the game's SPU2 sound RAM (PS2X_SPU2_RAM_MB); above 2 the driver is patched for "
                         f"{SOUND_ENTRIES} effects per bank and every enemy keeps its sounds (default 2: the PS2's)")
    args = ap.parse_args()

    psxdata = args.disc / "PSXDATA"
    backup = args.backup or (args.disc.parent / "disc_world_original")
    if not psxdata.is_dir():
        print(f"cross-world: no PSXDATA folder in {args.disc}")
        return 2
    if args.restore:
        n = restore(args.disc, backup) if backup.is_dir() else 0
        print(f"cross-world: original world files restored ({n} changed back)")
        return 0
    if not (args.randomizer / "randomizer" / "cross_world.py").is_file():
        print(f"cross-world: the randomizer is not in {args.randomizer} (randomizer\\cross_world.py is missing)")
        return 2

    sys.path.insert(0, str(args.randomizer))
    import randomizer  # noqa: E402  (the randomizer's own code)
    from randomizer import catalog, cross_world, sound_port, sound_bank  # noqa: E402

    detail = open(args.log, "w", encoding="utf-8") if args.log else None

    def log(msg) -> None:
        if detail:
            detail.write(f"{msg}\n")

    # One run at a time: a second launcher pressing Play meanwhile would write the same files.
    backup.mkdir(parents=True, exist_ok=True)
    lock = backup / "crossworld.lock"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > 600:
            lock.unlink()  # left behind by a run that was killed
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        print("cross-world: another run is already adding the enemies", flush=True)
        return 3
    try:
        return run(args, psxdata, backup, randomizer, catalog, cross_world, sound_port, sound_bank, log, detail)
    finally:
        lock.unlink(missing_ok=True)


def run(args, psxdata, backup, randomizer, catalog, cross_world, sound_port, sound_bank, log, detail) -> int:
    from randomizer import bef as _bef, befscript as _bs
    save_originals(args.disc, backup)
    restore(args.disc, backup)  # every run starts from the originals: nothing of an earlier run is left over
    originals = backup / "PSXDATA"
    allow = catalog.load_allowlist()
    ceiling = int(0x640000 * args.pool_scale * OCCUPANCY)
    from randomizer import texture_port
    texture_port.CLUT_TABLE_SIZE = max(175, min(440, args.clut_table))
    summary = []
    written = []
    failed = False

    # Sound limits: the PS2's, or the larger SPU2 RAM with the patched driver.
    sound_note_full = "the rest are silent: SPU RAM is full"
    if args.sound_ram_mb > 2:
        max_sfx = sound_bank.MAX_SFX
        try:
            patched, note = patch_sound_table((backup / DRIVER).read_bytes(), SOUND_ENTRIES)
            write(args.disc / DRIVER, patched)
            written.append(DRIVER.as_posix())
            max_sfx = SOUND_ENTRIES
            print(f"cross-world: sound driver: {note}", flush=True)
        except (OSError, IrxPatchError) as e:
            print(f"cross-world: sound driver left as it is ({e}); banks stay below 300 effects", flush=True)
        sound_ceiling = args.sound_ram_mb * 1024 * 1024 - sound_bank.SPU_BASE
        sound_bank.MAX_SFX = max_sfx
        sound_bank.SPU_USABLE_CEILING = sound_ceiling
        sound_bank.ceiling_for = lambda *a, **k: sound_ceiling
        _merge = sound_bank.merge
        sound_bank.merge = lambda *a, **k: _merge(*a, **{**k, "max_sfx": max_sfx})
        sound_note_full = f"the rest are silent: {max_sfx} effects or {args.sound_ram_mb} MB reached"

    with tempfile.TemporaryDirectory(prefix="maximo_crossworld_") as tmp:
        work = Path(tmp)
        # The randomizer reads every world's trio and bank by plain file name from one folder, always the originals.
        for folder, stem in WORLDS.values():
            for ext in ("BEF", "PRS", "PRT"):
                shutil.copy2(originals / folder / f"{stem}.{ext}", work / f"{stem}.{ext}")
            if (originals / "SOUND" / f"{stem}.BD").is_file():
                shutil.copy2(originals / "SOUND" / f"{stem}.BD", work / f"{stem}.BD")
        # The boss arenas and their banks, under the names the randomizer looks for, from the originals (arenas are
        # targets now too), plus untouched copies in src/ that the bosses are always ported from.
        src_dir = work / "src"
        src_dir.mkdir()
        for _tid, _name, arena, bank in BOSSES:
            for ext in ("BEF", "PRS", "PRT"):
                shutil.copy2(originals / ARENA_FOLDER[arena] / f"{arena}.{ext}", work / f"{arena}.{ext}")
                shutil.copy2(originals / ARENA_FOLDER[arena] / f"{arena}.{ext}", src_dir / f"{arena}.{ext}")
            if (originals / "SOUND" / f"{bank}.BD").is_file():
                shutil.copy2(originals / "SOUND" / f"{bank}.BD", work / f"{arena}.BD")
        bosses_module = load_boss_module(randomizer, ceiling) if args.bosses else None
        # Grim's stand-in arena: his classes, with CONTINUE.PRS/.PRT.
        template = _bef.BefFile.parse(work / f"{TEMPLATE[0]}.BEF").get_entry(TEMPLATE[1]).data
        (work / "GRIM.BEF").write_bytes(make_grim_bef(_bef, psxdata / "GAME" / "CONTINUE.BEF", template))
        shutil.copy2(psxdata / "GAME" / "CONTINUE.PRS", work / "GRIM.PRS")
        shutil.copy2(psxdata / "GAME" / "CONTINUE.PRT", work / "GRIM.PRT")
        # (port type, name, arena, renamed to, remove GameVarAdd, sound source type)
        jobs = [(tid, bname, arena, None, False, tid) for tid, bname, arena, _bank in BOSSES]
        jobs.append((GRIM_TYPES[0], "Grim", "GRIM", None, True, None))
        jobs += [(0x99, f"Sorceress ({ARENA_FOLDER[a].title()})", a, new, True, 0x99)
                 for a, new in SORCERESS_TYPES.items()]

        targets = [(w, w, folder, stem, stem, None) for w, (folder, stem) in WORLDS.items()]
        if getattr(args, "arenas", True):
            targets += [t for t in EXTRA_TARGETS if (work / f"{t[3]}.BEF").is_file()]
        for label, world, folder, stem, bank, second_world in targets:
            print(f"cross-world: {label.upper()}...", flush=True)
            kept_results = []  # what the arena's extra passes ported and kept (they get sounds too)
            session = cross_world.WorldPortSession(world, work, log=log, archive_stem=stem)
            ported, refused = [], []
            for tid in sorted({e.enemy_id for e in allow.values() if e.target_world == world}):
                name = catalog.name_for_type(tid)
                if any(t == tid for t, *_ in session.companion_blobs):
                    ported.append(name)  # already brought along as a companion of an earlier route
                    continue
                ok, why = cross_world.port_enemy_route(session, type_id=tid, game_files_dir=work,
                                                       allowlist=allow, emit=log)
                (ported if ok else refused).append(name if ok else f"{name} ({why})")
            # Structures (the debug menu's SPAWN STRUCTURE): each type the randomizer measured as shippable into this
            # world, when it still fits the resource pool. Heaviest budget check is the live session size.
            if getattr(args, "structures", True):
                struct_ok, struct_skipped = [], []
                try:
                    from randomizer import roster_costs as _rc
                    struct_allow = catalog.load_structure_allowlist()
                    struct_types = sorted({e.enemy_id for e in struct_allow.values() if e.target_world == world})
                except Exception as e:  # noqa: BLE001 - structures are optional; enemies and bosses still go ahead
                    struct_types = []
                    print(f"cross-world: {world.upper()}: structures unavailable ({e})", flush=True)
                for tid in struct_types:
                    entry = catalog.structure_allowlist_lookup(tid, world, struct_allow)
                    row = _rc.structure_cost_for(tid, entry.source_world, world) if entry else None
                    if entry is None or row is None:
                        continue
                    if any(t == tid for t, *_ in session.companion_blobs):
                        struct_ok.append(entry.name)
                        continue
                    if len(session.prs) + len(session.prt) + int(row.resident_bytes) > ceiling:
                        struct_skipped.append(f"{entry.name} (pool full)")
                        continue
                    ok, why = cross_world.port_structure_route(
                        session, type_id=tid, name=entry.name, source_world=entry.source_world,
                        companions=tuple(row.companions), game_files_dir=work, emit=log)
                    (struct_ok if ok else struct_skipped).append(entry.name if ok else f"{entry.name} ({why})")
                print(f"cross-world: {world.upper()}: structures {len(struct_ok)} ported"
                      + (f", skipped: {'; '.join(struct_skipped)}" if struct_skipped else ""), flush=True)
            size = len(session.prs) + len(session.prt)
            if size > ceiling:
                failed = True
                summary.append(f"{label}: NOT CHANGED, {size:,} B of models and textures would pass "
                               f"{ceiling:,} B of the expanded resource pool")
                continue
            report = cross_world.verify_session(session)
            if not getattr(report, "ok", False):
                failed = True
                summary.append(f"{label}: NOT CHANGED, the randomizer's checks failed: "
                               f"{'; '.join(map(str, getattr(report, 'failures', [])))[:300]}")
                continue
            for fname, data in session.finish().items():
                (work / fname).write_bytes(data)  # the world as the enemies left it; the bosses go on top
            if second_world:
                # The natives pass: the enemies of the arena's own world, through routes into another world.
                natives = sorted({e.enemy_id for e in allow.values()
                                  if e.target_world == second_world and e.source_world == world})
                if natives:
                    session2 = cross_world.WorldPortSession(second_world, work, log=log, archive_stem=stem)
                    for tid in natives:
                        name = catalog.name_for_type(tid)
                        if name in ported or any(t == tid for t, *_ in session2.companion_blobs):
                            continue
                        ok, why = cross_world.port_enemy_route(session2, type_id=tid, game_files_dir=work,
                                                               allowlist=allow, emit=log)
                        (ported if ok else refused).append(name if ok else f"{name} ({why})")
                    size2 = len(session2.prs) + len(session2.prt)
                    report2 = cross_world.verify_session(session2)
                    if size2 <= ceiling and getattr(report2, "ok", False):
                        for fname, data in session2.finish().items():
                            (work / fname).write_bytes(data)
                        kept_results += list(session2.results)
                    else:
                        refused.append(f"{world} enemies (natives pass not kept: "
                                       f"{'pool full' if size2 > ceiling else 'checks failed'})")

            # The common enemies: Basic/Sword/Guard/Axe Skeletons and the Basic Zombie (with its Torso Zombie) live in
            # every world, so no allowlist route carries them, but an arena's archive has none of them. They are
            # ported straight from a world that has them, through the same gated port path with a PORT plan of
            # their own (no companions: the Torso Zombie is ported as a class of its own).
            if second_world:
                commons_src = "castle" if world != "castle" else "grave"
                session3 = cross_world.WorldPortSession(world, work, log=log, archive_stem=stem)
                have = {e.type_id for e in _bef.BefFile.parse(work / f"{stem}.BEF").entries}
                src_bef = _bef.BefFile.parse(work / f"{commons_src.upper()}.BEF")
                got3 = []
                try:
                    from randomizer import companion_port as _cp
                    oracle3 = _cp.make_spawn_edge_oracle(src_bef, _bs.REVISION_RETAIL)
                except Exception as e:  # noqa: BLE001 - without it the commons go alone, as before
                    _cp, oracle3 = None, None
                    log(f"commons -> {stem}: no spawn-edge oracle ({e}); ported without companions")
                for tid in COMMON_ENEMIES + ARENA_EXTRAS:
                    if tid in have or not src_bef.has_type(tid):
                        continue
                    if any(t == tid for t, *_ in session3.companion_blobs):
                        got3.append(catalog.name_for_type(tid))  # already brought along by an earlier common enemy
                        continue
                    plan = cross_world.PlacementPlan(enemy_id=tid, name=catalog.name_for_type(tid), target_world=world,
                                                     path="PORT", source_world=commons_src, status="CANDIDATE",
                                                     reason="")
                    before = len(session3.results)
                    # With what it spawns: the Axe Skeleton's axe (0x36), the Guard Skeleton's 0x42, the Basic Skeleton's
                    # and Basic Zombie's 0x35, the zombie's Torso Zombie. Ported alone, the skeletons came into the
                    # arenas without those pieces and lost part of their animation.
                    grouped = False
                    if _cp is not None:
                        try:
                            group = _cp.resolve_group(tid, commons_src, src_bef, oracle3)
                            members = tuple(n.type_id for n in group.members)
                            outcome = _cp.port_group(session3, plan, src_bef.get_entry(tid).data, source_bef=src_bef,
                                                     edges=oracle3, companions=members)
                            grouped = True
                            log(f"commons -> {stem}: 0x{tid:02X} with companions "
                                f"{[f'0x{m:02X}' for m in members]}: {'ok' if outcome.ok else outcome.reason}")
                        except Exception as e:  # noqa: BLE001 - fall back to the enemy alone
                            log(f"commons -> {stem}: 0x{tid:02X} companions not resolved ({e}); ported alone")
                    if not grouped:
                        session3.port_enemy(plan, src_bef.get_entry(tid).data)
                    if any(r.ok for r in session3.results[before:]):
                        got3.append(plan.name)
                    else:
                        refused.append(f"{plan.name} (common enemy refused)")
                size3 = len(session3.prs) + len(session3.prt)
                report3 = cross_world.verify_session(session3)
                if got3 and size3 <= ceiling and getattr(report3, "ok", False):
                    for fname, data in session3.finish().items():
                        (work / fname).write_bytes(data)
                    kept_results += list(session3.results)
                    ported += got3
                elif got3:
                    refused.append(f"common enemies (not kept: {'pool full' if size3 > ceiling else 'checks failed'})")
                del src_bef

            # Bosses, each with everything it spawns, one after the other into the same world files.
            boss_members, boss_names, boss_refused = [], [], []
            for tid, bname, arena, rename, game_vars, sound_type in (jobs if bosses_module else ()):
                if arena == stem:
                    continue  # the arena's own boss (and sorceress) is already there
                ok, msg, out, members = port_boss(
                    bosses_module, _bef, _bs, boss_type=tid,
                    arena_dir_files={e: (src_dir if (src_dir / f"{arena}.{e}").is_file() else work) / f"{arena}.{e}"
                                     for e in ("BEF", "PRS", "PRT")},
                    target_files={e: work / f"{stem}.{e}" for e in ("BEF", "PRS", "PRT")}, log=log,
                    rename=rename, game_vars=game_vars, template=template)
                log(f"boss {bname} -> {world}: {'ok' if ok else 'REFUSED'}: {msg}")
                if ok and out is not None and len(out["PRS"]) + len(out["PRT"]) > ceiling:
                    ok, msg = False, f"would pass {ceiling:,} B of the expanded resource pool"
                if not ok:
                    boss_refused.append(f"{bname} ({msg})")
                    continue
                if out is not None:
                    for ext, data in out.items():
                        (work / f"{stem}.{ext}").write_bytes(data)
                boss_names.append(bname)
                if sound_type is not None:  # sounds by the SOURCE classes (a renamed sorceress is still 0x99 there)
                    boss_members += [SimpleNamespace(type_id=m, source_world=arena.lower(),
                                                     name=f"{bname} 0x{m:02X}") for m in members]
            prt = (work / f"{stem}.PRT").read_bytes()
            protect = struct.unpack_from("<I", (originals / folder / f"{stem}.PRT").read_bytes(), 12)[0]
            prt, converted = fit_palettes(prt, 175, protect, texture_port)
            if converted:
                (work / f"{stem}.PRT").write_bytes(prt)
                log(f"{world}: {converted} ported texture(s) made 24-bit to keep 175 palettes")
            size = (work / f"{stem}.PRS").stat().st_size + len(prt)

            for ext in ("BEF", "PRS", "PRT"):
                data = (work / f"{stem}.{ext}").read_bytes()
                if data != (originals / folder / f"{stem}.{ext}").read_bytes():
                    write(psxdata / folder / f"{stem}.{ext}", data)
                    written.append(f"PSXDATA/{folder}/{stem}.{ext}")

            # Sound: the foreign samples that fit the world's bank.
            admitted, seen = [], set()
            for r in list(session.results) + kept_results:
                if r.ok and r.plan.enemy_id not in seen:
                    seen.add(r.plan.enemy_id)
                    admitted.append(SimpleNamespace(type_id=r.plan.enemy_id,
                                                    source_world=r.plan.source_world or "", name=r.plan.name))
            sound_note = "sounds unchanged"
            admitted += boss_members
            if admitted and bank and (work / f"{stem}.BD").is_file():
                # An arena merges into ITS OWN bank (BS_GRAVE...: copied to the work folder as GRAVE_B.BD...). It used
                # to merge into its world's bank, which then replaced the arena's: the arena's boss, sorceress and
                # other sounds of its own were lost.
                bank_target = stem.lower() if second_world else world
                bd, merge, _ = sound_port.merge_for_lineup(admitted, game_files_dir=work, target_world=bank_target,
                                                           lineup=None, log=log)
                if args.sound_ram_mb > 2:
                    # Every bank gets every sound of the game (647 effects, about 6 MB: room to spare in the larger
                    # sound RAM and the patched driver's 1000). The randomizer can only import the sound ids it reads
                    # in an enemy's script; an id the script works out while it runs (bosses, the skeletons' and
                    # zombies' random voices) stayed missing and that sound was silent.
                    filled, added = fill_bank(bd if bd is not None else (work / f"{stem}.BD").read_bytes(),
                                              originals / "SOUND", sound_bank)
                    if added:
                        log(f"{label}: {added} more effect(s) from the other banks")
                        bd = filled
                if bd is not None:
                    base = md5(originals / "SOUND" / f"{bank}.BD")
                    for sound in SOUND_DIRS:
                        orig = originals / sound / f"{bank}.BD"
                        if orig.is_file() and md5(orig) == base:
                            write(psxdata / sound / f"{bank}.BD", bd)
                            written.append(f"PSXDATA/{sound}/{bank}.BD")
                    refused_sound = list(getattr(merge, "refusals", ()) or ())
                    try:
                        final_bank = sound_bank.parse(bd)
                        sound_note = (f"sound bank {len(final_bank.samples):,} B of samples, {final_bank.count} effects"
                                      + (f" ({sound_note_full})" if refused_sound else ""))
                    except Exception as e:  # noqa: BLE001 - only the summary line
                        sound_note = f"sound bank written ({e})"
            print(f"cross-world: {label.upper()} done, +{len(ported)} enemies, +{len(boss_names)} bosses and NPCs", flush=True)
            summary.append(f"{label}: +{len(ported)} enemies ({', '.join(ported) or 'none'}); "
                           f"+{len(boss_names)} bosses and NPCs ({', '.join(boss_names) or 'none'}); "
                           f"{size / 1048576:.1f} MB of models and textures; {sound_note}"
                           + (f"; refused: {', '.join(refused + boss_refused)}" if refused or boss_refused else ""))

    if detail:
        detail.close()
    for line in summary:
        print(f"cross-world: {line}", flush=True)
    stamp_lines = [f"version={VERSION}-{args.sound_ram_mb}-{args.clut_table}-{args.pool_scale}"] + [f"{rel}={md5(args.disc / rel)}" for rel in written]
    (backup / STAMP).write_text("\n".join(stamp_lines) + "\n", encoding="utf-8")
    print(f"cross-world: {len(written)} file(s) written to {psxdata}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
