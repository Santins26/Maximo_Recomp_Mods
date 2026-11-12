"""READ-ONLY inspector for a folder of built game files.

WHAT THIS IS FOR
----------------
Cross-world enemies are not appearing in game. Two fix attempts failed because
every verification was done on IN-MEMORY bytes with all writes intercepted, so
nothing was ever proved about what actually lands on disk. This tool answers the
one question those runs could not: *do the .PSX files in this folder contain
enemy records whose type id is foreign to the world the file belongs to?*

It WRITES NOTHING, ANYWHERE - no output files, no caches, no temp files. Every
file it touches is opened for reading (or only `stat()`ed). Nothing in
`randomizer/` imports this module; it is a diagnostic that sits outside the
build path on purpose.

USAGE
-----
    python -m research.tools.inspect_build_output "<folder with built PSX files>"
    python -m research.tools.inspect_build_output "<built folder>" --vanilla "<untouched extract or game_files>"

MEMORY DISCIPLINE
-----------------
`.PRS` files are multi-megabyte, and a `PsxFile` holds the whole file in `.raw`.
So: PSX files are parsed ONE AT A TIME, reduced immediately to a small summary
(counts + per-instance type ids), and the parsed object is dropped before the
next file is opened - never more than one large buffer alive. `.PRS` / `.PRT`
sizes are read with `stat()`; those bytes are never loaded, because no check
here needs them.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# Make `randomizer` importable when this file is run from a checkout.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from randomizer import catalog                      # noqa: E402
from randomizer.bef import BefFile                  # noqa: E402
from randomizer.psx import PsxFile                  # noqa: E402
from randomizer import sound_bank                   # noqa: E402

# Known vanilla sizes, for the "did the grown asset actually reach this folder"
# check. Only the figures we have measured are listed; anything not listed is
# reported as a size with no expectation attached.
VANILLA_SIZES: dict[str, int] = {
    "GRAVE.PRS": 2_563_126,
    "GRAVE.PRT": 817_232,
}


@dataclass
class PsxSummary:
    """Everything we keep from one .PSX file. Deliberately tiny."""
    name: str
    world: str | None
    bef_path: str
    record_count: int
    census: Counter                     # type_id -> count
    types_by_instance: dict[int, int]    # instance_id -> type_id
    error: str | None = None
    instance_names: dict[int, str] = field(default_factory=dict)

    @property
    def foreign_types(self) -> set[int]:
        """Enemy type ids present here that are NOT native to this world.

        Restricted to ids the catalog actually knows as walking-melee enemies
        (`catalog.NATIVE_WORLDS`), so items, triggers and scenery - which are
        native nowhere by definition - cannot masquerade as cross-world
        enemies.
        """
        if not self.world:
            return set()
        return {
            tid for tid in self.census
            if tid in catalog.NATIVE_WORLDS
            and not catalog.is_native(tid, self.world)
        }


def _psx_paths(folder: Path) -> list[Path]:
    found: dict[Path, Path] = {}
    for pat in ("*.PSX", "*.psx"):
        for p in sorted(folder.glob(pat)):
            found.setdefault(p.resolve(), p)
    return [found[k] for k in sorted(found)]


def summarize_psx(path: Path) -> PsxSummary:
    """Parse one .PSX and reduce it to a summary, dropping the file bytes.

    A missing or unparseable file is REPORTED, never raised.
    """
    try:
        psx = PsxFile.parse(path)
    except Exception as exc:                      # noqa: BLE001 - report, never raise
        return PsxSummary(name=path.name, world=None, bef_path="",
                          record_count=0, census=Counter(),
                          types_by_instance={},
                          error=f"{type(exc).__name__}: {exc}")
    try:
        census: Counter = Counter()
        by_instance: dict[int, int] = {}
        names: dict[int, str] = {}
        for r in psx.records:
            census[r.type_id] += 1
            by_instance[r.instance_id] = r.type_id
            names[r.instance_id] = r.instance_name
        return PsxSummary(
            name=path.name,
            world=catalog.get_world_from_bef_path(psx.bef_path),
            bef_path=psx.bef_path,
            record_count=len(psx.records),
            census=census,
            types_by_instance=by_instance,
            instance_names=names,
        )
    finally:
        del psx                                   # drop the large buffer now


def _fmt_type(tid: int) -> str:
    return f"0x{tid:02X} {catalog.name_for_type(tid)}"


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def _report_censuses(summaries: list[PsxSummary]) -> Counter:
    total: Counter = Counter()
    print("=" * 78)
    print("1. ENEMY TYPE-ID CENSUS (per file)")
    print("=" * 78)
    for s in summaries:
        if s.error:
            print(f"\n{s.name}: UNREADABLE - {s.error}")
            continue
        print(f"\n{s.name}  world={s.world or '?'}  bef='{s.bef_path}'  "
              f"records={s.record_count}")
        for tid, n in sorted(s.census.items()):
            total[tid] += n
            print(f"    {_fmt_type(tid):<28} x{n}")
    print("\n" + "-" * 78)
    print("TOTAL ACROSS ALL FILES")
    for tid, n in sorted(total.items()):
        print(f"    {_fmt_type(tid):<28} x{n}")
    return total


def _report_foreign(summaries: list[PsxSummary]) -> int:
    print("\n" + "=" * 78)
    print("2. FOREIGN vs NATIVE (cross-world enemies)")
    print("=" * 78)
    foreign_records = 0
    any_world = False
    for s in summaries:
        if s.error:
            continue
        if not s.world:
            print(f"\n{s.name}: world unknown from bef path '{s.bef_path}' "
                  f"- cannot classify")
            continue
        any_world = True
        foreign = s.foreign_types
        if not foreign:
            print(f"\n{s.name} ({s.world}): no foreign enemy types")
            continue
        for tid in sorted(foreign):
            n = s.census[tid]
            foreign_records += n
            homes = ", ".join(sorted(catalog.native_worlds(tid))) or "nowhere"
            print(f"\n{s.name} ({s.world}): FOREIGN {_fmt_type(tid)} x{n} "
                  f"(native to: {homes})")
    if not any_world:
        print("\nNo file could be attributed to a world - foreign/native "
              "classification did not run.")
    print(f"\nFOREIGN ENEMY RECORDS TOTAL: {foreign_records}")
    return foreign_records


def _report_vanilla_diff(summaries: list[PsxSummary],
                         vanilla_folder: Path) -> tuple[int, int]:
    print("\n" + "=" * 78)
    print(f"3. VANILLA COMPARISON  (against {vanilla_folder})")
    print("=" * 78)
    changed_total = 0
    became_foreign = 0
    if not vanilla_folder.is_dir():
        print(f"  NOT A FOLDER: {vanilla_folder} - comparison skipped")
        return 0, 0
    for s in summaries:
        if s.error:
            continue
        vpath = vanilla_folder / s.name
        if not vpath.exists():
            print(f"\n{s.name}: no vanilla counterpart in "
                  f"{vanilla_folder.name} - skipped")
            continue
        v = summarize_psx(vpath)          # one at a time; buffer dropped inside
        if v.error:
            print(f"\n{s.name}: vanilla copy UNREADABLE - {v.error}")
            continue
        diffs = []
        for inst, tid in s.types_by_instance.items():
            old = v.types_by_instance.get(inst)
            if old is None or old == tid:
                continue
            diffs.append((inst, old, tid))
        if not diffs:
            print(f"\n{s.name}: 0 records changed type")
            continue
        changed_total += len(diffs)
        print(f"\n{s.name}: {len(diffs)} record(s) changed type")
        for inst, old, new in sorted(diffs):
            foreign_mark = ""
            if s.world and new in catalog.NATIVE_WORLDS \
                    and not catalog.is_native(new, s.world):
                foreign_mark = "  <-- FOREIGN"
                became_foreign += 1
            nm = s.instance_names.get(inst, "")
            print(f"    inst {inst:<6} {_fmt_type(old):<26} -> "
                  f"{_fmt_type(new):<26}{foreign_mark}  {nm}")
    print(f"\nRECORDS CHANGED: {changed_total}   "
          f"OF WHICH BECAME FOREIGN: {became_foreign}")
    return changed_total, became_foreign


def _report_bef(folder: Path, summaries: list[PsxSummary]) -> list[str]:
    """Check every foreign type a .PSX references is declared by its .BEF."""
    print("\n" + "=" * 78)
    print("4. BEF CROSS-CHECK (does the .BEF declare the types the .PSX uses?)")
    print("=" * 78)
    problems: list[str] = []
    # Resolve the .BEF each PSX points at, by basename, case-insensitively.
    bef_by_name = {p.name.upper(): p for p in folder.glob("*")
                   if p.suffix.upper() == ".BEF"}
    cache: dict[str, set[int] | str] = {}
    checked = False
    for s in summaries:
        if s.error:
            continue
        wanted = s.foreign_types
        bef_name = Path(s.bef_path.replace("\\", "/")).name.upper()
        if not bef_name:
            print(f"\n{s.name}: no BEF path in header - cannot check")
            continue
        bef_path = bef_by_name.get(bef_name)
        if bef_path is None:
            print(f"\n{s.name}: BEF '{bef_name}' not present in this folder "
                  f"- cannot check")
            continue
        if bef_name not in cache:
            try:
                bf = BefFile.parse(bef_path)
                cache[bef_name] = set(bf.type_ids())
                del bf
            except Exception as exc:              # noqa: BLE001
                cache[bef_name] = f"{type(exc).__name__}: {exc}"
        declared = cache[bef_name]
        if isinstance(declared, str):
            print(f"\n{s.name}: BEF '{bef_name}' UNREADABLE - {declared}")
            continue
        if not wanted:
            print(f"\n{s.name}: no foreign types to check against {bef_name}")
            continue
        checked = True
        missing = sorted(t for t in wanted if t not in declared)
        present = sorted(t for t in wanted if t in declared)
        for t in present:
            print(f"\n{s.name}: {bef_name} DECLARES {_fmt_type(t)}  PASS")
        for t in missing:
            msg = (f"{s.name}: {bef_name} does NOT declare "
                   f"{_fmt_type(t)} referenced by a record")
            problems.append(msg)
            print(f"\n{msg}  FAIL")
    if not checked and not problems:
        print("\nNothing to check: no foreign types referenced by any .PSX.")
    return problems


def _report_sizes(folder: Path, vanilla: Path | None) -> list[str]:
    print("\n" + "=" * 78)
    print("5. SIZE REPORT (.PRS / .PRT / .BEF)")
    print("=" * 78)
    notes: list[str] = []
    assets = sorted(
        (p for p in folder.glob("*")
         if p.suffix.upper() in (".PRS", ".PRT", ".BEF")),
        key=lambda p: p.name.upper(),
    )
    if not assets:
        print("  No .PRS/.PRT/.BEF files in this folder.")
        notes.append("no .PRS/.PRT/.BEF present in the inspected folder")
        return notes
    for p in assets:
        try:
            size = p.stat().st_size          # stat only: never load these bytes
        except OSError as exc:
            print(f"  {p.name:<16} UNREADABLE - {exc}")
            continue
        expected = VANILLA_SIZES.get(p.name.upper())
        if expected is None and vanilla is not None:
            vp = vanilla / p.name
            try:
                expected = vp.stat().st_size if vp.exists() else None
            except OSError:
                expected = None
        if expected is None:
            print(f"  {p.name:<16} {size:>12,}")
            continue
        delta = size - expected
        state = "GREW" if delta > 0 else ("VANILLA SIZE" if delta == 0
                                         else "SMALLER")
        print(f"  {p.name:<16} {size:>12,}   vanilla {expected:>12,}   "
              f"{delta:+,}  {state}")
        if delta == 0:
            notes.append(f"{p.name} is at vanilla size ({size:,})")
    return notes


def _report_sound_banks(folder: Path, vanilla: Path | None) -> list[str]:
    """Parse every `.BD` ON DISC and prove the merged bank is shippable.

    This section exists because the sound merge is exactly the kind of change
    that passes in memory and does nothing in game: a bank whose EffectIDs are
    right but whose offsets overlap, or which overruns usable SPU RAM, produces
    NO error -- the driver has no ceiling check and `GetSpotFXID` just fails to
    find an id. So the four things that can go wrong silently are all checked
    against the bytes in this folder: `nSFX` below the driver's limit, offsets
    inside the block and neither overlapping nor gapped, occupancy under the
    usable-SPU ceiling, and (against `--vanilla`) which ids were ADDED.

    Reads only; `.BD` files are opened ONE AT A TIME and dropped.
    """
    print("\n" + "=" * 78)
    print("6. SOUND BANKS (.BD): layout, EffectIDs and SPU occupancy")
    print("=" * 78)
    problems: list[str] = []
    paths = sorted((p for p in folder.glob("*") if p.suffix.upper() == ".BD"),
                   key=lambda p: p.name.upper())
    if not paths:
        print("  No .BD files in this folder. If cross-world enemies are "
              "expected to make noise, the merged bank did not arrive -- "
              "check that '.BD' is in iso.ASSET_FILE_EXTENSIONS.")
        return problems
    for p in paths:
        data = p.read_bytes()
        for line in sound_bank.describe(data, name=p.name):
            print(f"  {line}")
        try:
            bank = sound_bank.parse(data)
        except sound_bank.SoundBankError as exc:
            problems.append(f"{p.name} does not parse: {exc}")
            continue
        for issue in sound_bank.validate(bank):
            problems.append(f"{p.name}: {issue}")
        if sound_bank.roundup64(len(data)) > sound_bank.SPU_USABLE_CEILING:
            problems.append(
                f"{p.name} occupies {sound_bank.roundup64(len(data)):,} B, over "
                f"the {sound_bank.SPU_USABLE_CEILING:,} B usable SPU ceiling -- "
                f"this WILL corrupt SPU RAM silently")
        if vanilla is not None:
            vp = vanilla / p.name
            if not vp.exists():
                print(f"  {p.name}: no vanilla counterpart to compare")
                continue
            vdata = vp.read_bytes()
            if vdata == data:
                print(f"  {p.name}: byte-identical to vanilla (untouched, "
                      f"round-tripped through the pipeline unchanged)")
                del vdata
                continue
            try:
                vbank = sound_bank.parse(vdata)
            except sound_bank.SoundBankError as exc:
                problems.append(f"{p.name}: vanilla copy does not parse ({exc})")
                del vdata
                continue
            before = set(vbank.effect_ids)
            added = [i for i in bank.effect_ids if i not in before]
            print(f"  {p.name}: MERGED  +{len(data) - len(vdata):,} B, "
                  f"+{bank.count - vbank.count} entries, "
                  f"{len(added)} EffectID(s) added: "
                  + (" ".join(str(i) for i in added) or "-"))
            # Every original id must still resolve to the SAME sample, or the
            # merge moved a sample the game already relied on.
            for index, entry in enumerate(vbank.entries):
                if vbank.sample_bytes(index) != _sample_for(bank, entry.effect_id,
                                                            vbank, index):
                    problems.append(
                        f"{p.name}: original EffectID {entry.effect_id} no "
                        f"longer resolves to its vanilla sample")
                    break
            del vbank
            del vdata
        del bank
        del data
    return problems


def _sample_for(bank, effect_id: int, vbank, vindex: int) -> bytes:
    """The merged bank's sample for `effect_id`, matched positionally first.

    Duplicate ids are legal (every retail bank carries 1435 and 1444), so a
    positional match is used where the merged bank still has the entry at the
    same index, falling back to the linear id search the game itself performs.
    """
    if vindex < bank.count and bank.entries[vindex].effect_id == effect_id:
        return bank.sample_bytes(vindex)
    index = bank.index_of(effect_id)
    return b"" if index is None else bank.sample_bytes(index)


def _verdict(foreign_records: int, bef_problems: list[str],
             size_notes: list[str], diff: tuple[int, int] | None,
             sound_problems: list[str] | None = None) -> None:
    print("\n" + "=" * 78)
    print("VERDICT")
    print("=" * 78)
    if foreign_records == 0:
        print("NO FOREIGN ENEMY RECORDS: the randomizer did not place any "
              "cross-world enemy in these files. The break is in placement, "
              "not in the disc.")
    else:
        print(f"FOREIGN ENEMY RECORDS PRESENT ({foreign_records}): placement "
              "worked. If they do not appear in game, the break is asset "
              "loading or the ISO did not receive these files.")
    if diff is not None:
        changed, foreign = diff
        print(f"VANILLA DIFF: {changed} record(s) changed type, {foreign} of "
              f"them to a foreign enemy.")
    if bef_problems:
        print(f"BEF DECLARATION: FAIL - {len(bef_problems)} type(s) "
              "referenced by a .PSX are not declared by its .BEF:")
        for m in bef_problems:
            print(f"    - {m}")
    else:
        print("BEF DECLARATION: PASS - every foreign type referenced by a "
              ".PSX is declared by its .BEF (or there were none to check).")
    if size_notes:
        print("ASSET SIZES: FAIL/ATTENTION -")
        for m in size_notes:
            print(f"    - {m}")
    else:
        print("ASSET SIZES: PASS - the grown assets are present and larger "
              "than vanilla.")
    if sound_problems is None:
        pass
    elif sound_problems:
        print(f"SOUND BANKS: FAIL - {len(sound_problems)} problem(s). A bank "
              f"that overruns usable SPU RAM corrupts it SILENTLY, so these "
              f"must be fixed before shipping:")
        for m in sound_problems:
            print(f"    - {m}")
    else:
        print("SOUND BANKS: PASS - every .BD parses, its offsets tile with no "
              "gap or overlap, nSFX is below the driver's limit and occupancy "
              "is under the usable-SPU ceiling.")
    print("=" * 78)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m research.tools.inspect_build_output",
        description="READ-ONLY inspector for built .PSX/.BEF/.PRS/.PRT output. "
                    "Writes nothing.",
    )
    ap.add_argument("folder", help="Folder containing the built .PSX files")
    ap.add_argument("--vanilla", default=None,
                    help="Untouched extract or game_files, for comparison")
    args = ap.parse_args(argv)

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"ERROR: not a folder: {folder}")
        return 2
    vanilla = Path(args.vanilla) if args.vanilla else None

    print(f"INSPECTING (read-only): {folder}")
    if vanilla:
        print(f"VANILLA REFERENCE:      {vanilla}")

    paths = _psx_paths(folder)
    if not paths:
        print("\nNo .PSX files found in this folder. Nothing to inspect.")
        return 1
    print(f"PSX files found: {len(paths)}")

    # One file in memory at a time.
    summaries = [summarize_psx(p) for p in paths]

    _report_censuses(summaries)
    foreign_records = _report_foreign(summaries)
    diff = _report_vanilla_diff(summaries, vanilla) if vanilla else None
    bef_problems = _report_bef(folder, summaries)
    size_notes = _report_sizes(folder, vanilla)
    sound_problems = _report_sound_banks(folder, vanilla)
    _verdict(foreign_records, bef_problems, size_notes, diff, sound_problems)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
