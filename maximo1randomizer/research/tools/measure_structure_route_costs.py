"""OFFLINE structure-route measurement harness (Task 3, `cross-world-structures`).

WHAT THIS IS
    A RESEARCH TOOL, the structure sibling of `measure_route_costs.py`. Nothing
    in `randomizer/` imports it, it is part of no build, and it WRITES NOTHING:
    every row goes to stdout for a human to read into the spec's measurement
    document. It reads game files and holds buffers in memory only.

WHY IT IS A SEPARATE FILE RATHER THAN A FLAG ON THE ENEMY HARNESS
    `measure_route_costs.py` drives `cross_world.run_cross_world_placements`,
    which calls `classify_placement`, whose step 1 refuses anything outside
    `WALKING_MELEE_POOL`. Every structure classifies NOT_PORT there, so that
    driver cannot measure one. Task 1 of this spec established the alternative
    and the repo already uses it: `port_enemy` is entity-generic, so a structure
    is ported by HAND-BUILDING a `PlacementPlan` and calling
    `session.port_enemy` / `companion_port.port_group` directly -- exactly what
    `cross_world._run_companion_experiment` does. No gate is relaxed: the mesh
    gate, the anim trust gate, the rig bind check, the texture remap, the
    append-only BEF injection and `verify_session` all run unmodified.

COMPANIONS
    A structure's companion set is not in any allowlist, so the closure comes
    from `roster.derive_companions` (the Task 1 oracle of the enemy roster
    spec, calibrated 11/11 against the catalog's hand-measured literals) and is
    ported through `companion_port.port_group`, so a row is the whole GROUP's
    cost. `catalog.CROSS_WORLD_ALLOWLIST` is NOT rebound: `port_group` is handed
    the companion tuple directly, so no allowlist row is invented anywhere.

MEMORY / FILE DISCIPLINE
    ONE ROUTE PER PROCESS. The driver parses no binary itself; each child
    touches only the six files the port path itself needs (source `.BEF`, source
    `.PRS`/`.PRT` read transiently, and the target `.PRS`/`.PRT`/`.BEF` the
    session holds). The script survey mode parses ONE `.BEF` per process.

USAGE
    every route into every foreign world (spawns one child per route):
        python -m research.tools.measure_structure_route_costs
    one route, this process:
        python -m research.tools.measure_structure_route_costs \
            --route 0x7C --source ice --target grave
    Script_Section instance-name survey, one world per process:
        python -m research.tools.measure_structure_route_costs --survey ice
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

GAME_FILES = REPO_ROOT / "game_files"

WORLDS = ("grave", "ice", "swamp", "castle", "under")

#: MEASURED home world per portable structure: the ONLY world whose `.BEF` type
#: table declares it, read out of the five `.BEF` files (3 + 2 across two
#: processes, to respect the four-binaries-per-process ceiling) and held here as
#: literals so the DRIVER process parses nothing. Matches requirements.md's
#: inventory table exactly, minus the excluded and ineligible ids.
#:
#: THE SEVEN LATE ADDITIONS (`0x01`, `0x02`, `0x0A`, `0xAE`, `0xB5`, `0xC8`,
#: `0xD3`) were resolved the same way and NOT from their type names: each id was
#: looked up in every world's `.BEF` type table (GRAVE/ICE/SWAMP in one process,
#: CASTLE/UNDER in a second, so no process parsed more than three binaries) and
#: EXACTLY ONE world declared each. The reading agreed with requirements.md's
#: inventory table in all seven cases, so there was nothing to stop and report:
#:     GRAVE declares 0x01 0x02 0x0A     ICE declares none of the seven
#:     SWAMP declares 0xB5 0xC8 0xD3     CASTLE declares none of the seven
#:     UNDER declares 0xAE
HOME_WORLD: dict = {
    0x01: "grave", 0x02: "grave", 0x0A: "grave",
    0x30: "grave", 0xA0: "grave", 0xB0: "grave", 0xC9: "grave",
    0x17: "ice", 0x18: "ice", 0x1B: "ice", 0x7C: "ice", 0xB1: "ice",
    0x4A: "castle", 0x50: "castle", 0xAA: "castle", 0xAD: "castle",
    0xBC: "castle",
    0xB5: "swamp", 0xBB: "swamp", 0xC8: "swamp", 0xD3: "swamp",
    0x6C: "under", 0xAE: "under",
}

#: Vanilla `.PRS`/`.PRT` sizes per world, from `measure_route_costs.VANILLA_SIZES`
#: -- asserted in every child so a mis-read target world is LOUD.
VANILLA_SIZES: dict = {
    "grave": (2_563_126, 817_232),
    "castle": (4_199_541, 1_633_184),
    "swamp": (3_442_632, 1_255_072),
    "ice": (3_082_301, 1_154_816),
    "under": (3_641_513, 1_352_448),
}


# --- one route, in this process --------------------------------------------

def measure_route(type_id: int, source_world: str, target_world: str,
                  game_files_dir: Path, *, verbose: bool = False) -> dict:
    """Measure ONE structure route in THIS process. Never raises."""
    from randomizer import befscript
    from randomizer import companion_port
    from randomizer import cross_world as cw
    from randomizer import roster
    from randomizer.bef import BefFile

    logs: list = []
    emit = (lambda m: print(f"    | {m}")) if verbose else logs.append

    names = roster.structure_candidates()
    row = {
        "type_id": type_id, "name": names.get(type_id, f"0x{type_id:02X}"),
        "source_world": source_world, "target_world": target_world,
        "prs_bytes": 0, "prt_bytes": 0, "bef_bytes": 0,
        "meshes": 0, "anims": 0, "textures": 0,
        "companions": [], "companions_injected": 0,
        "missed_local_refs": 0, "blob_len": 0,
        "status": "ERROR", "gate": "", "reason": "", "verified": None,
    }

    reason = roster.structure_exclusion_reason(type_id)
    if reason is not None:
        row["status"] = "EXCLUDED"
        row["gate"] = "S9"
        row["reason"] = reason
        return row

    source_path = Path(game_files_dir) / f"{source_world.upper()}.BEF"
    if not source_path.is_file():
        row["reason"] = f"{source_path.name} is missing"
        return row
    source_bef = BefFile.parse(source_path)
    if not source_bef.has_type(type_id):
        row["status"] = "NO_BLOB"
        row["gate"] = "S1"
        row["reason"] = f"{source_path.name} declares no type 0x{type_id:02X}"
        return row
    blob = source_bef.get_entry(type_id).data
    row["blob_len"] = len(blob)
    row["missed_local_refs"] = len(companion_port.missed_local_refs(blob))

    closure, why = roster.derive_companions(type_id, source_world, source_bef)
    if why is not None:
        row["status"] = "NO_CLOSURE"
        row["gate"] = "S10"
        row["reason"] = why
        return row
    companions = tuple(sorted(int(c) for c in closure))
    row["companions"] = list(companions)

    session = cw.WorldPortSession(target_world, game_files_dir, log=emit)
    plan = cw.PlacementPlan(
        enemy_id=type_id, name=row["name"], target_world=target_world,
        path=cw.PORT, source_world=source_world, status="MEASUREMENT")
    oracle = companion_port.make_spawn_edge_oracle(source_bef,
                                                   befscript.REVISION_RETAIL)
    companion_port.port_group(session, plan, blob, source_bef=source_bef,
                              edges=oracle, companions=companions)
    session.finish()

    expect = VANILLA_SIZES.get(target_world)
    if expect is not None:
        assert len(session.pristine_prs) == expect[0], len(session.pristine_prs)
        assert len(session.pristine_prt) == expect[1], len(session.pristine_prt)

    row["prs_bytes"] = len(session.prs) - len(session.pristine_prs)
    row["prt_bytes"] = len(session.prt) - len(session.pristine_prt)
    row["bef_bytes"] = len(session.bef) - len(session.pristine_bef)

    results = list(session.results)
    refused = [r for r in results if r.refused]
    committed = [r for r in results if r.ok]
    memo = list(getattr(session, "companion_memo", {}).values())
    if committed and not refused:
        row["status"] = "COMMIT"
        row["meshes"] = int(sum(r.ported_meshes for r in committed)
                            + sum(c.ported_meshes for c in memo))
        row["textures"] = int(sum(r.ported_textures for r in committed)
                              + sum(c.ported_textures for c in memo))
        row["anims"] = int(sum(r.appended_anims for r in committed)
                           + sum(c.appended_anims for c in memo))
        row["companions_injected"] = int(
            sum(getattr(r, "companion_injected", 0) for r in committed))
        report = cw.verify_session(session, [plan])
        row["verified"] = bool(report.ok)
        if not report.ok:
            row["status"] = "VERIFY_FAIL"
            row["gate"] = "S5"
            row["reason"] = "; ".join(report.failures)
    else:
        row["status"] = "REFUSE"
        row["gate"] = "gate"
        row["reason"] = " | ".join(
            r.plan.reason for r in refused if r.plan.reason
        ) or "refused with no reason recorded"

    row["log_tail"] = logs[-6:]
    del session
    return row


# --- Script_Section instance-name survey (one .BEF per process) ------------

#: An instance name in this game's `.PSX` records is a short lowercase-ish token
#: ending in a digit (`key1`, `coffin3`, `end2`), which is exactly the shape the
#: ordered puzzle chains index. A blob whose own Script_Section carries such a
#: literal is depending on a name the level file supplies, so it cannot travel.
_NAMEISH = re.compile(rb"[A-Za-z][A-Za-z_]{2,15}\d{1,2}\x00")
_STRING = re.compile(rb"[ -~]{4,}")

#: API slots that make a script depend on something OUTSIDE its own entity: other
#: entities by lookup, or level / sub-level progress state. These are the closest
#: decidable proxy for "does this script need a chain of siblings the foreign
#: world has not got", and the four ordered puzzle props act as the controls.
_LEVEL_STATE_SLOTS = frozenset({
    66,    # CheckSubLevelCompleted
    84,    # FindApparitions   -- entity lookup
    106, 107, 108,   # GameVarSub / GameVarRead / GameVarSet -- persistent
                     # cross-entity state, which is how a chain remembers where
                     # it got to; SWAMP 0xC8 Spirit_Statue (a known-bad control)
                     # uses GameVarRead x3 + GameVarSet
    155, 156, 157, 158,   # GetEntityLoadClass* / GetLoadClass*
    165,   # IsSubLevelCompleted
    202,   # GetEntity         -- entity lookup
    221,   # GetLevelID
})


def survey_world(world: str, game_files_dir: Path) -> list:
    """Report instance-name-shaped literals in each candidate's script section."""
    from randomizer import befscript
    from randomizer import companion_port
    from randomizer import roster
    from randomizer.bef import BefFile

    bef = BefFile.parse(Path(game_files_dir) / f"{world.upper()}.BEF")
    out = []
    for type_id, name in sorted(roster.structure_candidates().items()):
        if not bef.has_type(type_id):
            continue
        blob = bef.get_entry(type_id).data
        section = befscript.script_section(blob)
        hay = section if section else b""
        row = {
            "type_id": type_id, "name": name, "world": world,
            "portable": roster.structure_exclusion_reason(type_id) is None,
            "script_bytes": len(hay),
            "nameish": sorted({m.group(0)[:-1].decode("ascii")
                               for m in _NAMEISH.finditer(hay)}),
            # The WHOLE blob as well as the script section: a name the script
            # consumes could sit in the blob's data area rather than in the
            # decoded instruction stream, and a survey that only looked at one
            # of the two would be able to claim a clean result it did not earn.
            "blob_nameish": sorted({m.group(0)[:-1].decode("ascii")
                                    for m in _NAMEISH.finditer(blob)}),
            "blob_strings": sorted({m.group(0).decode("ascii")
                                    for m in _STRING.finditer(blob)})[:12],
        }
        # WHAT THE SCRIPT ASKS THE WORLD FOR. The string scan above found NO
        # ASCII name literal in ANY blob, including the four ordered puzzle
        # props that are known to depend on level-driven chains -- so instance
        # names are supplied by the LEVEL (`.PSX` records), not carried in the
        # entity blob, and a literal scan cannot discriminate. The decidable
        # question the blob CAN answer is which API slots it calls: a script that
        # reaches for other entities or for level/sub-level state is depending on
        # something a foreign world may not provide, and the four known-bad props
        # are the positive controls that calibrate the reading.
        slots: dict = {}
        if section:
            dis = befscript.disassemble(section, befscript.REVISION_RETAIL)
            for ins in dis.instructions:
                for op in ins.operands:
                    if (op.kind == "apiidx"
                            or op.name == befscript.API_INDEX_OPERAND_NAME):
                        slot = befscript.api_slot(op.value)
                        label = slot.name if slot else f"api[{op.value}]"
                        slots[f"{op.value}:{label}"] = slots.get(
                            f"{op.value}:{label}", 0) + 1
            row["decode_clean"] = bool(dis.clean)
            row["decode_coverage"] = round(dis.coverage, 4)
        row["missed_local_refs"] = len(companion_port.missed_local_refs(blob))
        row["api_calls"] = dict(sorted(slots.items(),
                                       key=lambda kv: int(kv[0].split(":")[0])))
        row["level_state_calls"] = sorted(
            k for k in slots if int(k.split(":")[0]) in _LEVEL_STATE_SLOTS)
        out.append(row)
    del bef
    return out


# --- driver ----------------------------------------------------------------

def _child(type_id: int, source_world: str, target_world: str):
    cmd = [sys.executable, "-m",
           "research.tools.measure_structure_route_costs",
           "--route", hex(type_id), "--source", source_world,
           "--target", target_world, "--emit-json"]
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True,
                          text=True)
    for line in proc.stdout.splitlines():
        if line.startswith("ROW "):
            return json.loads(line[4:])
    print(f"    !! child failed 0x{type_id:02X} {source_world}->"
          f"{target_world} (exit {proc.returncode})")
    for line in (proc.stdout.strip().splitlines()[-4:]
                 + proc.stderr.strip().splitlines()[-6:]):
        print(f"       {line}")
    return None


def _fmt(row: dict) -> str:
    comps = " ".join(f"0x{c:02X}" for c in row["companions"]) or "-"
    return (f"  0x{row['type_id']:02X} {row['name']:<22} "
            f"{row['source_world']:<6}->{row['target_world']:<6} "
            f"{row['status']:<11} {row['prs_bytes']:>8,} PRS "
            f"{row['prt_bytes']:>8,} PRT {row['bef_bytes']:>6,} BEF  "
            f"m{row['meshes']} a{row['anims']} t{row['textures']} "
            f"comp {comps}")


def run_driver(type_ids, targets) -> int:
    print("=" * 78)
    print("STRUCTURE ROUTE COST MEASUREMENT -- one route per process, "
          "nothing written")
    print("=" * 78)
    rows = []
    for type_id in type_ids:
        home = HOME_WORLD.get(type_id)
        if home is None:
            print(f"  0x{type_id:02X}: no measured home world; NO ROW")
            continue
        for target in targets:
            if target == home:
                continue
            print(f"  measuring 0x{type_id:02X} {home}->{target} ...",
                  flush=True)
            row = _child(type_id, home, target)
            if row is None:
                continue
            print(_fmt(row), flush=True)
            if row["reason"]:
                print(f"        {row['reason'][:240]}", flush=True)
            rows.append(row)
    print()
    print("=" * 78)
    print("TABLE")
    print("=" * 78)
    for row in rows:
        print(_fmt(row))
    return 0


def main(argv=None) -> int:
    from randomizer import roster

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route")
    parser.add_argument("--source")
    parser.add_argument("--target")
    parser.add_argument("--survey", help="world whose candidate scripts to scan")
    parser.add_argument("--emit-json", action="store_true")
    parser.add_argument("--game-files", default=str(GAME_FILES))
    args = parser.parse_args(argv)

    if args.survey:
        for row in survey_world(args.survey, Path(args.game_files)):
            if args.emit_json:
                print("SURVEY " + json.dumps(row))
            else:
                print(f"  0x{row['type_id']:02X} {row['name']:<22} "
                      f"script {row['script_bytes']:>5} B  "
                      f"portable={row['portable']}  "
                      f"nameish={row['nameish']} "
                      f"missed_refs={row['missed_local_refs']} "
                      f"nameish={row['nameish']}{row['blob_nameish']} "
                      f"level_state={row['level_state_calls']}")
        return 0

    if args.route:
        type_id = int(args.route, 0)
        source = args.source or HOME_WORLD.get(type_id)
        if source is None:
            print(f"no measured home world for 0x{type_id:02X}")
            return 2
        row = measure_route(type_id, source, args.target or "grave",
                            Path(args.game_files),
                            verbose=not args.emit_json)
        if args.emit_json:
            print("ROW " + json.dumps(row))
        else:
            print(_fmt(row))
            if row["reason"]:
                print(f"    {row['reason']}")
        return 0

    return run_driver(sorted(roster.portable_structure_sources()), WORLDS)


if __name__ == "__main__":
    raise SystemExit(main())
