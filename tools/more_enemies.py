#!/usr/bin/env python3
"""more_enemies.py - write copies of the Maximo: Ghosts to Glory level files (.PSX) with extra enemies.

WHY THIS IS A FILE TOOL AND NOT A RUNTIME PATCH
    A level's entity records sit in front of the (much larger) tail of the file, and the game sizes its load buffer
    from the file's size. Growing the data inside the running game would mean changing that size, shifting the tail
    and patching the loader. Loose .PSX files on disk can simply be written bigger, and the randomizer already shows
    that the records section may grow (psx.py: the post-records field is a SIZE, nothing else needs relocating).

WHAT IT DOES
    For every level file (*_SUB*.PSX by default) it copies some of the level's own walking enemies and appends the
    copies AFTER the last record, so every existing record keeps its entity number. It then raises the record count
    and the records-section size fields to match. Each copy gets a new instance id and name, and sits a short
    distance from the enemy it was copied from. A file is skipped, untouched, if its fields do not match what was
    parsed (the same checks the randomizer's measurements rest on).

WHAT IT DOES NOT DO
    It adds no new enemy TYPES, so no new assets are needed: every copy is a type the level already loads. It never
    touches boss arenas, hubs, sky or menu files. Nothing is changed in place: output goes to --out.

NEEDS THE OBJECT BUDGET
    The game stops running enemies past a per-level weight limit, so extra enemies only show up if the launcher's
    "Object limit" option (MAXIMO_OBJECT_BUDGET=more) is on.

Usage:
    python more_enemies.py --disc <folder with PSXDATA> --out <new folder> [--multiplier 1.5] [--dry-run]
"""
from __future__ import annotations

import argparse
import math
import re
import struct
import sys
from pathlib import Path

MASK64 = 0xFFFFFFFFFFFFFFFF

NAME_BUFFER_SIZE = 0x100
RECORD_HEADER_SIZE = 0x200
ENTITY_HEADER_SIZE = 0x28
ENTRY_SIZE = 0x20

# Walking enemies (spawn_config.ENEMY_TYPES in the randomizer). Not bosses, not Monster Generators, not items.
ENEMY_TYPES = {
    0x47: "Basic Skeleton", 0x2F: "Axe Skeleton", 0x39: "Sword Skeleton", 0x54: "Guard Skeleton",
    0x49: "Bomb Skeleton", 0x3A: "Pirate Skeleton", 0x48: "Basic Zombie", 0x6A: "Torso Zombie",
    0x10: "Frozen Zombie", 0x19: "Swamp Zombie", 0x0D: "Zombie Crocodile", 0x6B: "Ghost / Poltergeist",
    0x37: "Raven", 0x1D: "Snowman", 0x13: "Goat Devil", 0x27: "Hammer Devil", 0x5D: "Doomed Soul",
    0x08: "Plant Monster", 0x53: "Dark Knight", 0x58: "Crazed Prisoner", 0x16: "Axe Guard",
}

# The engine's per-level entity flag table has 384 entries (func_00120570 and friends in the PAL executable).
ENTITY_TABLE = 384
DEFAULT_MAX_RECORDS = 380
# maximo_boot.exe keeps the flags of entities past 384 itself when the object budget is raised.
EXTENDED_MAX_RECORDS = 1000

LEVEL_FILE = re.compile(r"(_SUB\d*|G_INTRO)\.PSX$", re.IGNORECASE)
# Names like Skeleton_12345 look generated; a hand-named instance (gate1_torch) is probably referenced by a script.
GENERATED_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*_\d+$")
# Every copy is named <original>_more<n>. A file that already has such a name was made by an earlier run: copying
# again would compound, so it is skipped.
COPY_SUFFIX = "_more"
COPY_NAME = re.compile(r"_more\d+x*$")


class Rng:
    """splitmix64, seeded from the text by FNV-1a. The launcher's C# port (launcher\\MoreEnemies.cs) is the same
    generator, so both tools write identical files for the same seed."""

    def __init__(self, text: str):
        h = 0xCBF29CE484222325
        for b in text.encode("ascii"):
            h = ((h ^ b) * 0x100000001B3) & MASK64
        self.state = h

    def next_u32(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        z ^= z >> 31
        return z >> 32

    def unit(self) -> float:
        return self.next_u32() / 4294967296.0

    def shuffle(self, items: list) -> None:
        for i in range(len(items) - 1, 0, -1):
            j = self.next_u32() % (i + 1)
            items[i], items[j] = items[j], items[i]


# ---------------------------------------------------------------------------------------------------------------
# PSX parsing (same layout detection as randomizer/psx.py)
# ---------------------------------------------------------------------------------------------------------------
class Record:
    __slots__ = ("offset", "class_name", "instance_name", "type_id", "instance_id", "prop_count", "pos", "raw")

    @property
    def size(self) -> int:
        return len(self.raw)


def _read_string(data: bytes, offset: int, max_len: int) -> str:
    end = offset
    while end < offset + max_len and end < len(data) and data[end] != 0:
        end += 1
    return data[offset:end].decode("latin-1")  # one char per byte, so names survive a round trip


def _is_class_buffer(data: bytes, offset: int) -> bool:
    if offset >= len(data):
        return False
    if not (0x41 <= data[offset] <= 0x5A):
        return False
    end = offset
    while end < offset + 64 and end < len(data) and 32 <= data[end] < 127:
        end += 1
    if end - offset < 3 or end >= len(data) or data[end] != 0:
        return False
    cd = 0
    for j in range(end + 1, min(offset + NAME_BUFFER_SIZE, len(data))):
        if data[j] != 0xCD:
            break
        cd += 1
    return cd >= 200


def _find_bef_path(data: bytes) -> int:
    for off in range(0, min(0x1200, len(data) - 5)):
        if data[off:off + 4].lower() == b".bef":
            start = off
            while start > 0 and start > off - 256:
                c = data[start - 1]
                if c == 0 or not (0x20 <= c < 127):
                    break
                start -= 1
            return start
    return 0


def detect_layout(data: bytes) -> dict:
    bef_off = _find_bef_path(data)
    if bef_off == 0:
        return {"count_offset": 0x530, "ptr_offset": 0x534, "records_start": 0x570}
    layout = {"count_offset": bef_off + 0x100, "ptr_offset": bef_off + 0x104, "records_start": bef_off + 0x140}
    if not _is_class_buffer(data, layout["records_start"]):
        for delta in (1, -1, 2, -2, 3, -3, 4, -4):
            if _is_class_buffer(data, layout["records_start"] + delta):
                for k in layout:
                    layout[k] += delta
                break
    return layout


def parse_records(data: bytes, records_start: int) -> list[Record]:
    records: list[Record] = []
    i = records_start
    while i < len(data) - RECORD_HEADER_SIZE - ENTITY_HEADER_SIZE:
        if not _is_class_buffer(data, i):
            i += 4
            continue
        end = i
        while end < i + 64 and 32 <= data[end] < 127:
            end += 1
        prop = i + RECORD_HEADER_SIZE
        r = Record()
        r.offset = i
        r.class_name = data[i:end].decode("ascii")
        r.instance_name = _read_string(data, i + NAME_BUFFER_SIZE, 256)
        r.type_id = data[prop + 0x04]
        r.instance_id = struct.unpack_from("<I", data, prop + 0x08)[0]
        r.prop_count = struct.unpack_from("<I", data, prop + 0x0C)[0]
        r.pos = struct.unpack_from("<fff", data, prop + 0x10)
        size = RECORD_HEADER_SIZE + ENTITY_HEADER_SIZE + r.prop_count * ENTRY_SIZE
        if not _is_class_buffer(data, i + size):
            test_i = i + max(size, 0x100)
            found = None
            while test_i < min(len(data) - 0x100, i + 0x10000):
                if _is_class_buffer(data, test_i):
                    found = test_i
                    break
                test_i += 4
            if found is not None:
                size = found - i
        r.raw = data[i:i + size]
        records.append(r)
        i += size
    return records


# ---------------------------------------------------------------------------------------------------------------
# Cloning
# ---------------------------------------------------------------------------------------------------------------
def _dist(a, b) -> float:
    dx, dy, dz = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return math.sqrt(dx * dx + dy * dy + dz * dz)  # written out so the C# port gives the same bits


def _nearest_distance(pos, others) -> float:
    best = math.inf
    for q in others:
        d = _dist(pos, q)
        if 0 < d < best:
            best = d
    return best


def _clone(src: Record, new_id: int, new_name: str, new_pos: tuple[float, float, float]) -> bytes:
    raw = bytearray(src.raw)
    prop = RECORD_HEADER_SIZE
    struct.pack_into("<I", raw, prop + 0x08, new_id)
    struct.pack_into("<fff", raw, prop + 0x10, *new_pos)
    # instance name: new text, then the original terminator padding is left as it was
    name = new_name.encode("latin-1")[:NAME_BUFFER_SIZE - 1]
    raw[NAME_BUFFER_SIZE:NAME_BUFFER_SIZE + len(name)] = name
    raw[NAME_BUFFER_SIZE + len(name)] = 0
    # trigger volumes (property type 0x0C) are in world coordinates: move them with the copy
    delta = tuple(n - o for n, o in zip(new_pos, src.pos))
    for k in range(src.prop_count):
        eo = RECORD_HEADER_SIZE + ENTITY_HEADER_SIZE + k * ENTRY_SIZE
        if eo + ENTRY_SIZE > len(raw):
            break
        if raw[eo] == 0x0C:
            c = struct.unpack_from("<3f", raw, eo + 8)
            struct.pack_into("<3f", raw, eo + 8, *(c[a] + delta[a] for a in range(3)))
    return bytes(raw)


def process_file(path: Path, multiplier: float, max_records: int, spread: float, up_axis: int, any_name: bool,
                 rng: Rng) -> tuple[bytes | None, str]:
    return transform(path.read_bytes(), multiplier, max_records, spread, up_axis, any_name, rng)


def transform(data: bytes, multiplier: float, max_records: int, spread: float, up_axis: int, any_name: bool,
              rng: Rng) -> tuple[bytes | None, str]:
    layout = detect_layout(data)
    records = parse_records(data, layout["records_start"])
    if not records:
        return None, "no records parsed"
    count_field = struct.unpack_from("<I", data, layout["count_offset"])[0]
    size_field = struct.unpack_from("<I", data, layout["ptr_offset"])[0]
    section_end = records[-1].offset + records[-1].size
    if count_field != len(records):
        return None, f"count field {count_field} != {len(records)} parsed records; skipped"
    if size_field != sum(r.size for r in records) or section_end - records[0].offset != size_field:
        return None, "records-section size field does not match the parsed records; skipped"

    if any(COPY_NAME.search(r.instance_name) for r in records):
        return None, "already has copies from an earlier run; skipped"
    enemies = [r for r in records if r.type_id in ENEMY_TYPES]
    if not enemies:
        return None, "no walking enemies"
    candidates = [r for r in enemies if any_name or GENERATED_NAME.match(r.instance_name)]
    want = int(round(len(enemies) * (multiplier - 1.0)))
    room = max_records - len(records)
    # Up to 9 copies per original (1 + 9 = 10x, the highest multiplier the launcher offers).
    extra = max(0, min(want, room, len(candidates) * 9))
    if extra == 0:
        return None, f"nothing to add ({len(enemies)} enemies, {len(candidates)} cloneable, room for {max(room, 0)})"

    # typical spacing between enemies decides how far a copy stands from its original
    positions = [r.pos for r in enemies]
    spacings = sorted(d for d in (_nearest_distance(p, positions) for p in positions) if d != math.inf)
    median_gap = spacings[len(spacings) // 2] if spacings else 1.0
    radius = max(median_gap * spread, 1e-3)
    horiz = [a for a in range(3) if a != up_axis]

    next_id = max(r.instance_id for r in records) + 1
    if next_id + extra > 0xFFFFFFFF:
        return None, "instance ids are too close to the 32-bit limit; skipped"
    used_names = {r.instance_name for r in records}
    picks = [candidates[i % len(candidates)] for i in range(extra)]
    rng.shuffle(picks)
    added = bytearray()
    for n, src in enumerate(picks):
        angle = rng.unit() * (2.0 * math.pi)
        pos = list(src.pos)
        pos[horiz[0]] += radius * math.cos(angle)
        pos[horiz[1]] += radius * math.sin(angle)
        name = f"{src.instance_name}{COPY_SUFFIX}{n + 1}"
        while name in used_names:
            name += "x"
        used_names.add(name)
        added += _clone(src, next_id + n, name, tuple(pos))

    out = bytearray(data[:section_end]) + added + bytearray(data[section_end:])
    struct.pack_into("<I", out, layout["count_offset"], len(records) + extra)
    struct.pack_into("<I", out, layout["ptr_offset"], size_field + len(added))

    # verify by parsing what was just built
    again = parse_records(bytes(out), layout["records_start"])
    ok = (len(again) == len(records) + extra
          and all(a.raw == b.raw for a, b in zip(records, again))
          and len({r.instance_id for r in again}) == len({r.instance_id for r in records}) + extra
          and struct.unpack_from("<I", out, layout["ptr_offset"])[0] == sum(r.size for r in again)
          and bytes(out[section_end + len(added):]) == data[section_end:])
    if not ok:
        return None, "self-check failed on the rebuilt file; skipped"
    return bytes(out), (f"{len(enemies)} enemies -> +{extra} copies (records {len(records)} -> {len(records) + extra}, "
                        f"+{len(added)} bytes, spacing {median_gap:.1f}, radius {radius:.1f})")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--disc", required=True, help="folder that contains PSXDATA (for example dist\\disc)")
    ap.add_argument("--out", required=True, help="new folder for the modified files (same PSXDATA layout)")
    ap.add_argument("--multiplier", type=float, default=1.5, help="enemy count factor per level (default 1.5 = +50%%)")
    ap.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS,
                    help=f"never go above this many records in a file (engine table is {ENTITY_TABLE}; default {DEFAULT_MAX_RECORDS})")
    ap.add_argument("--extended-table", action="store_true",
                    help=f"the game keeps entities past {ENTITY_TABLE} (maximo_boot.exe with the object budget raised): "
                         f"allows --max-records up to {EXTENDED_MAX_RECORDS}")
    ap.add_argument("--spread", type=float, default=0.5, help="copy distance as a fraction of the typical enemy spacing")
    ap.add_argument("--up-axis", type=int, choices=(0, 1, 2), default=1, help="vertical axis of the position floats (default 1 = Y)")
    # Instance names are designer area labels shared by unrelated objects, not script references: copy every enemy.
    ap.add_argument("--any-name", dest="any_name", action="store_true", default=True,
                    help="copy every walking enemy (default)")
    ap.add_argument("--generated-only", dest="any_name", action="store_false",
                    help="only copy enemies whose names look generated (Name_1234)")
    ap.add_argument("--files", default="", help="comma list of file names to process instead of every *_SUB*.PSX")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    a = ap.parse_args(argv)
    if not 1.0 < a.multiplier <= 10.0:
        print("--multiplier must be above 1.0 and at most 10.0")
        return 2
    if not 0 < a.max_records <= (EXTENDED_MAX_RECORDS if a.extended_table else ENTITY_TABLE):
        print(f"--max-records must be 1..{ENTITY_TABLE} (1..{EXTENDED_MAX_RECORDS} with --extended-table)")
        return 2

    disc, out = Path(a.disc), Path(a.out)
    root = disc / "PSXDATA"
    if not root.is_dir():
        print(f"no PSXDATA folder in {disc}")
        return 2
    if not a.dry_run and out.resolve() == disc.resolve():
        print("--out must be a different folder from --disc")
        return 2
    wanted = {f.strip().upper() for f in a.files.split(",") if f.strip()}
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.upper() == ".PSX"
                   and ((p.name.upper() in wanted) if wanted else LEVEL_FILE.search(p.name)))
    if not files:
        print("no level files found")
        return 1
    written = 0
    for p in files:
        rel = p.relative_to(disc)
        rng = Rng(f"{a.seed}:{p.name.upper()}")
        data, msg = process_file(p, a.multiplier, a.max_records, a.spread, a.up_axis, a.any_name, rng)
        print(f"{p.name:14s} {msg}")
        if data is not None and not a.dry_run:
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            written += 1
    print(f"\n{written} file(s) written to {out}" if not a.dry_run else "\ndry run: nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
